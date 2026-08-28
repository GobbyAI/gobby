"""Tmux window naming and repair for persisted sessions."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from gobby.agents.tmux.session_manager import (
    TmuxProbeResult,
    TmuxReleaseOutcome,
    TmuxSessionManager,
)
from gobby.hooks.background_tasks import create_background_task
from gobby.sessions.tmux_context import get_tmux_manager_for_context, parse_terminal_context_value
from gobby.storage.sessions._title_defaults import format_provisional_session_title
from gobby.terminal_ownership import (
    TERMINAL_INACTIVE_STATUSES,
    TERMINAL_OWNER_STATUSES,
    OwnershipState,
    PaneOwnershipDecision,
    log_pane_ownership_decision,
    resolve_pane_ownership,
    terminal_session_identity,
)

logger = logging.getLogger(__name__)

_UNRESOLVED_SESSION_REF_RE = re.compile(
    r"(?<![a-z0-9_])(?:#session_ref|#\{session_ref\}|\{session_ref\})(?![a-z0-9_])"
)


def schedule_tmux_window_rename(
    session: Any,
    title: str,
    *,
    loop: Any | None = None,
) -> None:
    """Run ``_rename_tmux_window`` from sync code using the best available loop."""
    coro = _rename_tmux_window(session, title)

    try:
        running_loop = asyncio.get_running_loop()
        create_background_task(coro, loop=running_loop)
        return
    except RuntimeError:
        pass

    if loop is not None:
        try:
            loop_is_usable = not loop.is_closed()
        except Exception:
            loop_is_usable = False

        if loop_is_usable:
            try:
                asyncio.run_coroutine_threadsafe(coro, loop)
                return
            except Exception:
                logger.debug("Failed to schedule tmux rename on captured loop", exc_info=True)
                coro.close()
                return

    try:
        asyncio.run(coro)
    except Exception:
        logger.debug("Failed to run tmux rename synchronously", exc_info=True)


def _synthesize_fallback_title(session: object) -> str:
    seq_num = getattr(session, "seq_num", None)
    return format_provisional_session_title(seq_num) if isinstance(seq_num, int) else "(gobby)"


def _contains_unresolved_session_ref(value: Any) -> bool:
    return isinstance(value, str) and _UNRESOLVED_SESSION_REF_RE.search(value.lower()) is not None


def _resolve_window_title(session: Any, terminal_context: dict[str, Any], title: str) -> str:
    """Return persisted titles verbatim, with a deterministic provisional fallback."""
    del terminal_context
    if title and not _contains_unresolved_session_ref(title):
        return title
    return _synthesize_fallback_title(session)


def _tmux_manager_for_session(session: Any, terminal_context: dict[str, Any]) -> TmuxSessionManager:
    """Build a tmux manager for *session*'s recorded server context."""
    agent_depth = getattr(session, "agent_depth", 0) or 0
    default_socket_name = "gobby" if agent_depth > 0 else ""
    return get_tmux_manager_for_context(terminal_context, default_socket_name=default_socket_name)


async def probe_tmux_pane(session: Any) -> TmuxProbeResult | None:
    """Probe the tmux server and pane recorded for one persisted session."""
    tc = parse_terminal_context_value(getattr(session, "terminal_context", None))
    if not tc:
        return None
    pane = tc.get("tmux_pane")
    if not isinstance(pane, str) or not pane:
        return None
    return await _tmux_manager_for_session(session, tc).probe_target(pane)


async def _apply_window_rename(
    session: Any,
    terminal_context: dict[str, Any],
    pane: str,
    title: str,
) -> bool:
    """Rename *pane*'s window for *session*, logging the structured outcome.

    Failures are logged but never propagated. Returns True only when tmux
    confirms the rename was applied.
    """
    resolved = _resolve_window_title(session, terminal_context, title)
    ref = getattr(session, "ref", "?")
    socket = (
        terminal_context.get("tmux_socket_path")
        or terminal_context.get("tmux_socket_name")
        or "default"
    )
    try:
        mgr = _tmux_manager_for_session(session, terminal_context)
        applied = bool(await mgr.rename_window(pane, resolved))
    except Exception as e:
        logger.warning(
            "tmux window rename errored for %s pane=%s socket=%s: %s",
            ref,
            pane,
            socket,
            e,
        )
        return False
    if applied:
        logger.debug(
            "Renamed tmux window for %s pane=%s socket=%s title=%r",
            ref,
            pane,
            socket,
            resolved,
        )
    else:
        logger.debug(
            "tmux window rename did not apply for %s pane=%s socket=%s "
            "(target missing or tmux error)",
            ref,
            pane,
            socket,
        )
    return applied


async def _managed_window_name_needs_repair(
    mgr: Any,
    pane: str,
    session: Any,
    terminal_context: dict[str, Any],
) -> bool:
    getter = getattr(mgr, "get_window_name", None)
    if getter is None:
        return False
    try:
        window_name = await getter(pane)
    except Exception:
        logger.debug("Failed to read window name for pane %s", pane, exc_info=True)
        return False
    if _contains_unresolved_session_ref(window_name):
        return True
    if not isinstance(window_name, str):
        return False
    current = window_name.strip()
    if not current:
        return False
    persisted_title = getattr(session, "title", None)
    title = persisted_title if isinstance(persisted_title, str) else ""
    return _resolve_window_title(session, terminal_context, title) != current


async def _rename_tmux_window(session: Any, title: str) -> None:
    """Rename the tmux window after a persisted title update.

    Uses the tmux server recorded in terminal context when present. Falls back
    to the default user server for user sessions and Gobby's isolated socket for
    spawned agents.
    Failures are logged but never propagated.
    """
    has_persisted_identity = isinstance(getattr(session, "id", None), str)
    persisted_session = await _reload_persisted_session(session)
    if has_persisted_identity:
        if persisted_session is None:
            return
        session = persisted_session
        if getattr(session, "status", None) not in TERMINAL_OWNER_STATUSES:
            return
        ownership = await _resolve_tmux_pane_ownership(session)
        if ownership is None or not ownership.requested_session_owns_pane:
            return
        title = getattr(session, "title", None) or ""

    tc = parse_terminal_context_value(getattr(session, "terminal_context", None))
    if not tc:
        return
    pane = tc.get("tmux_pane")
    if not isinstance(pane, str) or not pane:
        return
    await _apply_window_rename(session, tc, pane, title)


async def _reload_persisted_session(session: Any) -> Any | None:
    """Reload a queued rename's session from daemon-owned storage."""
    session_id = getattr(session, "id", None)
    if not isinstance(session_id, str) or not session_id:
        return None

    from gobby.app_context import get_app_context

    container = get_app_context()
    if container is None or container.session_manager is None:
        return None
    session_manager = container.session_manager

    try:
        return await container.run_db(session_manager.get, session_id)
    except Exception:
        logger.debug("Failed to reload session %s before tmux rename", session_id, exc_info=True)
        return None


async def _resolve_tmux_pane_ownership(session: Any) -> PaneOwnershipDecision | None:
    """Reload every record for a pane and resolve its process-backed owner."""
    identity = terminal_session_identity(session)
    session_id = getattr(session, "id", None)
    if not isinstance(session_id, str):
        return None
    if identity is None:
        decision = PaneOwnershipDecision(
            None,
            session_id,
            None,
            "invalid_identity",
        )
        log_pane_ownership_decision(logger, decision)
        return decision

    from gobby.app_context import get_app_context

    container = get_app_context()
    session_manager = container.session_manager if container is not None else None
    candidates = [session]
    if (
        container is not None
        and session_manager is not None
        and hasattr(session_manager, "find_by_terminal_identity")
    ):
        try:
            loaded_candidates = await container.run_db(
                session_manager.find_by_terminal_identity,
                identity,
            )
            if loaded_candidates:
                candidates = list(loaded_candidates)
                if all(getattr(candidate, "id", None) != session_id for candidate in candidates):
                    candidates.append(session)
        except Exception:
            logger.debug(
                "Failed to load pane peers for session %s",
                session_id,
                exc_info=True,
            )
            return None

    decision = await asyncio.to_thread(
        resolve_pane_ownership,
        candidates,
        requested_session_id=session_id,
    )
    log_pane_ownership_decision(logger, decision)
    return decision


async def resolve_tmux_repair_owner(session: Any) -> Any | None:
    """Return the current canonical owner for periodic pane repair."""
    ownership = await _resolve_tmux_pane_ownership(session)
    return ownership.owner if ownership is not None else None


async def release_window_name_if_unowned(session: Any) -> bool:
    """Release stale Gobby title overrides recorded by inactive metadata."""
    persisted_session = await _reload_persisted_session(session)
    if persisted_session is None:
        return False
    session = persisted_session
    if getattr(session, "status", None) not in TERMINAL_INACTIVE_STATUSES:
        return False

    ownership = await _resolve_tmux_pane_ownership(session)
    if ownership is None or ownership.state is not OwnershipState.OWNERLESS:
        return False

    tc = parse_terminal_context_value(getattr(session, "terminal_context", None))
    if not tc:
        return False
    pane = tc.get("tmux_pane")
    if not isinstance(pane, str) or not pane:
        return False

    mgr = _tmux_manager_for_session(session, tc)
    try:
        outcome = await mgr.release_window_title_ownership(pane)
        return outcome in {
            TmuxReleaseOutcome.RELEASED,
            TmuxReleaseOutcome.ALREADY_RELEASED,
        }
    except Exception:
        logger.warning("Failed to release stale title for pane %s", pane, exc_info=True)
        return False


async def enforce_window_name_if_unmanaged(session: Any) -> bool:
    """Rename a tracked session's tmux window when unmanaged or visibly stale.

    Used by the periodic repair sweep. A window Gobby has already named has
    ``automatic-rename`` off (``rename_window`` disables it); such windows are
    left untouched only when their current title matches the authoritative
    persisted session title. Returns True when a rename was issued.

    This is the durable safety net for sessions whose session-start rename never
    lands — notably interactive Claude sessions in a VSCode tmux pane, which keep
    an empty title and otherwise stay frozen on the CLI's startup OSC window name
    (e.g. its version string).

    Returns False when terminal context is missing, the tmux pane is absent, the
    window cannot be inspected, or the window already matches the persisted
    session title.
    """
    has_persisted_identity = isinstance(getattr(session, "id", None), str)
    persisted_session = await _reload_persisted_session(session)
    if has_persisted_identity:
        if persisted_session is None:
            return False
        session = persisted_session
        if getattr(session, "status", None) not in TERMINAL_OWNER_STATUSES:
            return False
        ownership = await _resolve_tmux_pane_ownership(session)
        if ownership is None or not ownership.requested_session_owns_pane:
            return False

    tc = parse_terminal_context_value(getattr(session, "terminal_context", None))
    if not tc:
        return False
    pane = tc.get("tmux_pane")
    if not isinstance(pane, str) or not pane:
        return False

    mgr = _tmux_manager_for_session(session, tc)
    try:
        auto_rename = await mgr.get_window_automatic_rename(pane)
    except Exception:
        logger.debug("Failed to read automatic-rename for pane %s", pane, exc_info=True)
        return False
    # None -> window unreadable/gone; False -> already Gobby-managed. Bad names
    # from older builds are repaired even though they are already managed.
    if auto_rename is None:
        return False
    if auto_rename is False and not await _managed_window_name_needs_repair(mgr, pane, session, tc):
        return False

    title = getattr(session, "title", None) or ""
    return await _apply_window_rename(session, tc, pane, title)
