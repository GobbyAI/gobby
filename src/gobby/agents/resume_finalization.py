"""Shared finalization and notification helpers for durable agent resume."""

from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import Future
from typing import Any

from gobby.storage.agent_resume import (
    FinalizeDaemonResumeResult,
    finalize_daemon_resume,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager

logger = logging.getLogger(__name__)


def reconcile_completion_registry(
    result: FinalizeDaemonResumeResult,
    completion_registry: Any | None,
) -> None:
    """Move in-memory wait state after the durable subscriber transfer commits."""
    if completion_registry is None:
        return
    continuation_prompt = completion_registry.get_continuation_prompt(result.original_run_id)
    completion_registry.register(
        result.successor_run_id,
        subscribers=list(result.subscriber_session_ids),
        continuation_prompt=continuation_prompt,
    )
    completion_registry.cleanup(result.original_run_id)


def finalize_resume_handoff(
    db: HubDatabase,
    *,
    original_run_id: str,
    successor_run_id: str,
    child_session_id: str,
    completion_registry: Any | None = None,
) -> FinalizeDaemonResumeResult:
    """Commit the durable handoff, then reconcile event-loop-owned state."""
    result = finalize_daemon_resume(
        db,
        original_run_id=original_run_id,
        successor_run_id=successor_run_id,
        child_session_id=child_session_id,
    )
    reconcile_completion_registry(result, completion_registry)
    return result


async def finalize_resume_handoff_async(
    db: HubDatabase,
    *,
    original_run_id: str,
    successor_run_id: str,
    child_session_id: str,
    completion_registry: Any | None = None,
) -> FinalizeDaemonResumeResult:
    """Finalize from the registry-owning loop without blocking it on the DB.

    The fenced transaction runs in a worker thread; registry reconciliation
    then runs on the calling loop, which must be the registry owner.
    """
    result = await asyncio.to_thread(
        finalize_daemon_resume,
        db,
        original_run_id=original_run_id,
        successor_run_id=successor_run_id,
        child_session_id=child_session_id,
    )
    reconcile_completion_registry(result, completion_registry)
    return result


def finalize_resume_handoff_threadsafe(
    db: HubDatabase,
    *,
    original_run_id: str,
    successor_run_id: str,
    child_session_id: str,
    completion_registry: Any | None,
    registry_loop: asyncio.AbstractEventLoop | None,
    timeout_seconds: float = 5.0,
) -> FinalizeDaemonResumeResult:
    """Finalize from a hook worker and wait for registry-owner reconciliation."""
    result = finalize_daemon_resume(
        db,
        original_run_id=original_run_id,
        successor_run_id=successor_run_id,
        child_session_id=child_session_id,
    )
    if completion_registry is None:
        return result
    if registry_loop is None or not registry_loop.is_running():
        # The registry is event-loop-owned; mutating it from this worker
        # thread would race the owner. The durable transfer above already
        # committed, and startup recovery rebuilds in-memory wait state.
        logger.warning(
            "Skipping in-memory completion-registry reconciliation for %s: "
            "registry loop unavailable",
            result.successor_run_id,
        )
        return result

    completed: Future[None] = Future()

    def reconcile() -> None:
        try:
            reconcile_completion_registry(result, completion_registry)
        except BaseException as exc:
            completed.set_exception(exc)
        else:
            completed.set_result(None)

    registry_loop.call_soon_threadsafe(reconcile)
    completed.result(timeout=timeout_seconds)
    return result


def notify_parent_of_recovery(
    db: HubDatabase,
    *,
    child_session_id: str,
    parent_session_id: str,
    content: str,
    run_id: str,
    event: str,
    dedupe_key: str | None = None,
) -> bool:
    """Persist a recovery message for the parent session.

    When ``dedupe_key`` is provided (e.g. a per-boot marker), an identical
    (run, event, dedupe_key) message is created at most once, so periodic
    reconciliation passes cannot spam the parent. Returns whether a message
    was created.
    """
    metadata = {"event": event, "run_id": run_id, "child_session_id": child_session_id}
    if dedupe_key is not None:
        metadata["dedupe_key"] = dedupe_key
    payload = json.dumps(metadata, sort_keys=True)
    if dedupe_key is not None:
        existing = db.fetchone(
            """
            SELECT 1
            FROM inter_session_messages
            WHERE to_session = %s
              AND message_type = 'agent_recovery'
              AND metadata_json = %s
            LIMIT 1
            """,
            (parent_session_id, payload),
        )
        if existing is not None:
            return False
    InterSessionMessageManager(db).create_message(
        from_session=child_session_id,
        to_session=parent_session_id,
        content=content,
        priority="normal",
        message_type="agent_recovery",
        metadata_json=payload,
    )
    return True
