"""Runtime helpers for task-scoped build controls."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.agents.kill import kill_agent
from gobby.agents.terminal_delivery import (
    deliver_existing_terminal_run_in_scope,
    run_terminal_delivery_offload,
    shielded_terminal_delivery,
)
from gobby.build.results import BuildAgentSummary, BuildTaskSummary
from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES, AgentRun, LocalAgentRunManager
from gobby.storage.daemon_resume_keys import REAP_REQUESTED_AT_KEY
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._transitions import reset_current_non_ready_stage
from gobby.utils.datetime import parse_stored_datetime
from gobby.utils.machine_id import require_machine_id

logger = logging.getLogger(__name__)

ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS = 30


def _resolve_task_ref(
    task_manager: LocalTaskManager,
    input_ref: str,
    project_id: str,
) -> Task:
    try:
        resolved_id = task_manager.resolve_task_reference(input_ref, project_id)
        return task_manager.get_task(resolved_id, project_id=project_id)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"task ref not found: {input_ref}") from exc


def _affected_tasks(task_manager: LocalTaskManager, root: Task) -> list[Task]:
    if root.task_type != "epic":
        return [root]

    rows = task_manager.db.fetchall(
        """
        WITH RECURSIVE subtree(id, depth, path) AS (
            SELECT id, 0, ARRAY[id]
            FROM tasks
            WHERE id = %s
            UNION ALL
            SELECT child.id, parent.depth + 1, parent.path || child.id
            FROM tasks child
            JOIN subtree parent ON child.parent_task_id = parent.id
            WHERE parent.depth < 100
              AND NOT child.id = ANY(parent.path)
        )
        SELECT id
        FROM subtree
        """,
        (root.id,),
    )
    return [task_manager.get_task(row["id"]) for row in rows]


def _task_summaries(tasks: list[Task]) -> list[BuildTaskSummary]:
    return [
        BuildTaskSummary(
            task_id=task.id,
            ref=f"#{task.seq_num}" if task.seq_num else task.id,
            title=task.title,
            task_type=task.task_type,
        )
        for task in tasks
    ]


def _active_agents(db: HubDatabase, task_ids: list[str]) -> list[AgentRun]:
    return LocalAgentRunManager(db).list_active_global(task_ids=task_ids, limit=1000)


def _agent_summaries(agents: list[AgentRun]) -> list[BuildAgentSummary]:
    return [
        BuildAgentSummary(
            run_id=run.id,
            task_id=run.task_id,
            status=run.status,
            child_session_id=run.child_session_id,
            worktree_id=run.worktree_id,
            clone_id=run.clone_id,
        )
        for run in agents
    ]


async def _cancel_active_agents(
    db: HubDatabase,
    agents: list[AgentRun],
    *,
    services: object | None,
) -> None:
    lifecycle_monitor = getattr(services, "agent_lifecycle_monitor", None)
    completion_registry = getattr(services, "completion_registry", None)
    run_manager = LocalAgentRunManager(db)

    for run in agents:

        async def cancel_and_deliver(run: AgentRun = run) -> None:
            try:
                try:
                    result = await kill_agent(
                        run,
                        db,
                        signal_name="TERM",
                        timeout=5.0,
                        close_terminal=True,
                        terminal_services=getattr(services, "terminal_services", None),
                    )
                    if not result.get("success"):
                        logger.info(
                            "agent_kill_noop",
                            extra={"run_id": run.id, "result": result},
                        )
                except Exception as exc:
                    logger.warning("Failed to kill active build agent %s: %s", run.id, exc)

                if lifecycle_monitor is not None:
                    transitioned = await lifecycle_monitor.terminalize_cancelled_run(
                        run.id,
                        terminal_reason="user_cancelled",
                    )
                else:
                    transitioned = (
                        await run_terminal_delivery_offload(
                            run_manager.cancel,
                            run.id,
                            terminal_reason="user_cancelled",
                        )
                        is not None
                    )
                if not transitioned:
                    logger.debug("Agent %s was already terminal while stopping build", run.id)
            finally:
                await deliver_existing_terminal_run_in_scope(
                    db=db,
                    agent_run_manager=run_manager,
                    completion_registry=completion_registry,
                    run_id=run.id,
                    run_db=run_terminal_delivery_offload,
                )

        await shielded_terminal_delivery(run.id, cancel_and_deliver)


def _parked_daemon_stop_runs(db: HubDatabase, task_ids: list[str]) -> list[AgentRun]:
    """List unconsumed parked daemon-stop originals for the given tasks."""
    if not task_ids:
        return []
    return LocalAgentRunManager(db).list_daemon_stop_orphans(
        machine_id=require_machine_id(),
        max_age_hours=None,
        task_ids=task_ids,
        limit=1000,
    )


async def _give_up_parked_daemon_stop_runs(
    db: HubDatabase,
    parked: list[AgentRun],
    *,
    services: object | None,
) -> int:
    """Request immediate reaping of parked daemon-stop runs.

    Durable requests let the lifecycle reaper retry when immediate reaping is
    unavailable or fails.
    """
    if not parked:
        return 0
    run_manager = LocalAgentRunManager(db)
    requested_at = datetime.now(UTC).isoformat()
    for run in parked:
        run_manager.merge_resume_metadata(run.id, {REAP_REQUESTED_AT_KEY: requested_at})
    lifecycle_monitor = getattr(services, "agent_lifecycle_monitor", None)
    if lifecycle_monitor is None:
        logger.info(
            "No lifecycle monitor available; %d parked daemon-stop run(s) flagged "
            "for reap on the next lifecycle tick",
            len(parked),
        )
        return len(parked)
    try:
        await lifecycle_monitor.reap_daemon_stop_orphans()
    except Exception as exc:
        logger.warning(
            "Immediate reap of parked daemon-stop runs failed; "
            "the lifecycle reaper will retry flagged runs: %s",
            exc,
        )
    return len(parked)


def _clear_stale_dispatch_mutexes(
    db: HubDatabase,
    task_ids: list[str],
    *,
    now: datetime | None = None,
) -> int:
    mutexes = TaskDispatchMutexManager(db)
    resolved_now = now or datetime.now(UTC)
    cleared = 0
    active_run_ids = {run.id for run in LocalAgentRunManager(db).list_active_global(limit=1000)}
    for task_id in task_ids:
        mutex = mutexes.get_mutex(task_id)
        if mutex is None:
            continue
        if mutex.run_id:
            if mutex.run_id not in active_run_ids and mutexes.force_release(task_id):
                cleared += 1
            continue
        if _is_orphan_no_run_dispatch_mutex(mutex, now=resolved_now):
            if mutexes.force_release(task_id):
                cleared += 1
    return cleared


def _is_orphan_no_run_dispatch_mutex(mutex: Any, *, now: datetime) -> bool:
    if getattr(mutex, "lease_holder", None) != "dispatcher":
        return False
    if getattr(mutex, "run_id", None):
        return False

    lease_until = _parse_mutex_timestamp(getattr(mutex, "lease_until", None))
    if lease_until is not None:
        return lease_until < now

    updated_at = _parse_mutex_timestamp(getattr(mutex, "updated_at", None))
    if updated_at is None:
        return False
    return now - updated_at >= timedelta(seconds=ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS)


def _parse_mutex_timestamp(value: datetime | str | None) -> datetime | None:
    try:
        return parse_stored_datetime(value)
    except ValueError:
        return None


def _clear_dispatch_mutexes(db: HubDatabase, task_ids: list[str]) -> int:
    mutexes = TaskDispatchMutexManager(db)
    cleared = int(mutexes.sweep_expired())
    for task_id in task_ids:
        if mutexes.force_release(task_id):
            cleared += 1
    return cleared


def _release_stale_agent_claims(
    task_manager: LocalTaskManager,
    db: HubDatabase,
    tasks: list[Task],
) -> int:
    active_session_ids = {
        session_id
        for run in LocalAgentRunManager(db).list_active_global(limit=1000)
        for session_id in (run.child_session_id, run.claimed_session_id, run.parent_session_id)
        if session_id
    }
    released = 0
    for task in tasks:
        claim = task.claimed_by_session_id
        if not claim or claim in active_session_ids:
            continue
        if not _has_terminal_agent_claim(db, task.id, claim):
            continue
        task_manager.release_task_claim(task.id)
        released += 1
    return released


def _has_terminal_agent_claim(db: HubDatabase, task_id: str, session_id: str) -> bool:
    rows = db.fetchall(
        """
        SELECT status
        FROM agent_runs
        WHERE task_id = %s
          AND (
            child_session_id = %s
            OR claimed_session_id = %s
            OR parent_session_id = %s
          )
        """,
        (task_id, session_id, session_id, session_id),
    )
    return any(row["status"] not in ACTIVE_AGENT_RUN_STATUSES for row in rows)


def _reset_current_stages(db: HubDatabase, tasks: list[Task], *, reason: str) -> int:
    reset = 0
    for task in tasks:
        if reset_current_non_ready_stage(db, task.id, reason=reason, by_actor="build"):
            reset += 1
    return reset


def _reset_stoppable_stages(db: HubDatabase, tasks: list[Task], *, reason: str) -> int:
    reset = 0
    task_manager = LocalTaskManager(db)
    for task in tasks:
        row = task_manager.stage_states.current_stage(task.id)
        if row and row.state in {"in_progress", "needs_review"}:
            if reset_current_non_ready_stage(db, task.id, reason=reason, by_actor="build"):
                reset += 1
    return reset


def _clean_blockers(
    tasks: list[Task],
    agents: list[AgentRun],
    *,
    force: bool,
) -> list[str]:
    blockers: list[str] = []
    if not force:
        active_refs = [f"#{task.seq_num}" for task in tasks if task.allow_automation]
        if active_refs:
            blockers.append(
                "automation must be stopped before clean; active tasks: " + ", ".join(active_refs)
            )
        if agents:
            blockers.append(
                "live agents must be stopped before clean; active runs: "
                + ", ".join(run.id for run in agents)
            )
    return blockers
