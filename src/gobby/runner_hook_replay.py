"""Typed restart hook replay barrier and unresolved-session fencing."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from gobby.runner_lifecycle_agents import _run_db
from gobby.storage.agents import TERMINAL_AGENT_RUN_STATUSES

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger("gobby.runner_lifecycle")


@dataclass(frozen=True, slots=True)
class HookReplayBarrierOutcome:
    """Agent-settle and session-recovery decision from one barrier pass."""

    settled: bool
    session_recovery_safe: bool
    excluded_session_ids: frozenset[str] = frozenset()


async def _run_agent_hook_replay_barrier(
    runner: GobbyRunner,
    *,
    timeout_seconds: float = 5.0,
) -> HookReplayBarrierOutcome:
    """Replay hook ingress and classify every unresolved session identity."""
    agent_runner = getattr(runner, "agent_runner", None)
    http_server = getattr(runner, "http_server", None)
    app = getattr(http_server, "app", None)
    if app is None:
        return HookReplayBarrierOutcome(settled=True, session_recovery_safe=True)

    from gobby.hooks.inbox import drain_hook_inbox_barrier

    horizon = getattr(runner, "http_bound_at_ms", None)
    result = await drain_hook_inbox_barrier(
        app,
        timeout_seconds=timeout_seconds,
        restart_horizon_ms=horizon if isinstance(horizon, int) else None,
    )
    if not result.timed_out:
        return HookReplayBarrierOutcome(settled=True, session_recovery_safe=True)

    unresolved_run_ids = set(result.unresolved_run_ids)
    ordinary_session_ids: set[str] = set()
    excluded_child_session_ids: set[str] = set()
    exclusion_set_complete = True
    unresolved_session_ids = result.unresolved_session_ids
    session_manager = getattr(runner, "session_manager", None)
    if unresolved_session_ids and session_manager is None:
        logger.warning("Hook replay timed out while session services were unavailable")
        exclusion_set_complete = False
    elif session_manager is not None:
        for session_id in unresolved_session_ids:
            try:
                session = await _run_db(runner, session_manager.get, session_id)
            except Exception:
                logger.warning(
                    "Failed to load unresolved session %s",
                    session_id,
                    exc_info=True,
                )
                exclusion_set_complete = False
                continue
            if session is None:
                continue
            run_id = getattr(session, "agent_run_id", None)
            if isinstance(run_id, str) and run_id:
                unresolved_run_ids.add(run_id)
            else:
                ordinary_session_ids.add(session_id)

    if not unresolved_run_ids:
        logger.info(
            "Hook inbox replay timed out after replaying %d envelope(s); "
            "%d ordinary session identity/identities remain excluded "
            "(residue_hooks=%d live_hooks=%d receipts=%d)",
            result.replayed,
            len(ordinary_session_ids),
            result.residue_hook_count,
            result.live_hook_count,
            result.receipt_count,
        )
        return HookReplayBarrierOutcome(
            settled=True,
            session_recovery_safe=exclusion_set_complete,
            excluded_session_ids=frozenset(ordinary_session_ids),
        )
    if agent_runner is None:
        logger.warning("Hook replay timed out while agent services were unavailable")
        return HookReplayBarrierOutcome(
            settled=False,
            session_recovery_safe=False,
            excluded_session_ids=frozenset(ordinary_session_ids),
        )

    run_storage = agent_runner.run_storage
    active_run_ids: set[str] = set()
    terminal_run_ids: set[str] = set()
    missing_run_ids: set[str] = set()
    unclassified_run_ids: set[str] = set()
    for run_id in unresolved_run_ids:
        try:
            run = await _run_db(runner, run_storage.get, run_id)
        except Exception:
            logger.warning("Failed to load unresolved agent run %s", run_id, exc_info=True)
            unclassified_run_ids.add(run_id)
            exclusion_set_complete = False
            continue
        if run is None:
            missing_run_ids.add(run_id)
            continue
        if run.status in TERMINAL_AGENT_RUN_STATUSES:
            terminal_run_ids.add(run_id)
            continue
        child_session_id = getattr(run, "child_session_id", None)
        if isinstance(child_session_id, str) and child_session_id:
            excluded_child_session_ids.add(child_session_id)
        if run.status not in {"pending", "running"}:
            logger.warning(
                "Unclassified unresolved agent run %s with status %r",
                run_id,
                run.status,
            )
            unclassified_run_ids.add(run_id)
            continue
        await _run_db(
            runner,
            run_storage.merge_resume_metadata,
            run_id,
            {"reconciliation_pending": True},
        )
        active_run_ids.add(run_id)

    if terminal_run_ids or missing_run_ids:
        logger.info(
            "Agent hook replay barrier settled %d terminal and %d missing run reference(s)",
            len(terminal_run_ids),
            len(missing_run_ids),
        )
    settled = not active_run_ids and not unclassified_run_ids
    if not settled:
        logger.warning(
            "Agent hook replay barrier timed out with %d active fenced run(s) and "
            "%d unclassified run lookup(s) (runs=%s residue_hooks=%d live_hooks=%d "
            "receipts=%d)",
            len(active_run_ids),
            len(unclassified_run_ids),
            ",".join(sorted(active_run_ids)) or "-",
            result.residue_hook_count,
            result.live_hook_count,
            result.receipt_count,
        )
    return HookReplayBarrierOutcome(
        settled=settled,
        session_recovery_safe=exclusion_set_complete,
        excluded_session_ids=frozenset(ordinary_session_ids | excluded_child_session_ids),
    )
