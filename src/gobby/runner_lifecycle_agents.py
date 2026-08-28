"""Agent completion recovery and shared restart-replay primitives."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.agents.recovery_state import (
    is_daemon_stop_parked,
    is_reconciliation_pending,
)
from gobby.agents.srt_process_cleanup import reap_orphaned_srt_runner_process_trees
from gobby.events.completion_registry import wake_result_is_delivered
from gobby.storage.agents import (
    TERMINAL_AGENT_RUN_STATUSES,
    LocalAgentRunManager,
)
from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger("gobby.runner_lifecycle")


async def _run_db(
    runner: GobbyRunner,
    operation: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    db_executor = getattr(runner, "db_executor", None)
    if db_executor is not None:
        return await db_executor.run(operation, *args, **kwargs)
    return await asyncio.to_thread(operation, *args, **kwargs)


async def _rehydrate_active_agent_completion_subscribers(runner: GobbyRunner) -> int:
    """Restore durable subscribers for active runs into the live registry."""
    db = getattr(runner, "database", None)
    registry = getattr(runner, "completion_registry", None)
    if db is None or registry is None:
        return 0

    subscriber_manager = CompletionSubscriberManager(db)
    run_manager = LocalAgentRunManager(db)
    rehydrated = 0
    offset = 0
    while True:
        runs = await _run_db(
            runner,
            run_manager.list_active_for_machine,
            require_machine_id(),
            limit=_RUN_REPLAY_PAGE_SIZE,
            offset=offset,
        )
        if not runs:
            break
        for run in runs:
            if is_reconciliation_pending(run):
                continue
            subscribers = await _run_db(
                runner,
                subscriber_manager.get_completion_subscribers,
                run.id,
            )
            if not subscribers:
                continue
            registry.register(
                run.id,
                subscribers=subscribers,
                continuation_prompt=getattr(run, "continuation_prompt", None),
            )
            rehydrated += 1
        offset += len(runs)
        if len(runs) < _RUN_REPLAY_PAGE_SIZE:
            break
    return rehydrated


async def _cleanup_terminal_agent_completion_subscribers(runner: GobbyRunner) -> int:
    """Redeliver retained terminal notifications and remove acknowledged rows."""
    db = getattr(runner, "database", None)
    wake_dispatcher = getattr(runner, "wake_dispatcher", None)
    wake = getattr(wake_dispatcher, "wake", None)
    if db is None or not callable(wake):
        return 0

    subscriber_manager = CompletionSubscriberManager(db)
    run_manager = LocalAgentRunManager(db)
    delivered_count = 0
    completion_ids = await _run_db(runner, subscriber_manager.list_completion_ids)
    for run_id in completion_ids:
        run = await _run_db(runner, run_manager.get, run_id)
        if (
            run is None
            or run.status not in TERMINAL_AGENT_RUN_STATUSES
            or is_daemon_stop_parked(run)
        ):
            continue
        subscribers = await _run_db(
            runner,
            subscriber_manager.get_completion_subscribers,
            run.id,
        )
        acknowledged: list[str] = []
        payload = {"status": run.status, "run_id": run.id}
        message = f"Agent {run.id} reached terminal status {run.status}"
        from gobby.tasks.close_review_delivery import terminal_review_delivery

        review_delivery = await _run_db(runner, terminal_review_delivery, db, run.id)
        if review_delivery is not None:
            payload, message = review_delivery
        for session_id in subscribers:
            try:
                outcome = await wake(session_id, message, payload)
            except Exception:
                logger.warning(
                    "Terminal completion redelivery failed for session %s (run %s)",
                    session_id,
                    run.id,
                    exc_info=True,
                )
                continue
            if wake_result_is_delivered(outcome):
                acknowledged.append(session_id)
        if acknowledged:
            await _run_db(
                runner,
                subscriber_manager.remove_completion_subscribers,
                run.id,
                session_ids=acknowledged,
            )
            from gobby.tasks.close_review_delivery import mark_terminal_review_delivered

            await _run_db(
                runner,
                mark_terminal_review_delivered,
                db,
                payload,
                acknowledged,
            )
            delivered_count += len(acknowledged)
    return delivered_count


async def _reconcile_task_close_reviews_on_startup(runner: GobbyRunner) -> int:
    """Reconcile durable close-review intents without launching replacement agents."""
    db = getattr(runner, "database", None)
    wake_dispatcher = getattr(runner, "wake_dispatcher", None)
    wake = getattr(wake_dispatcher, "wake", None)
    if db is None:
        return 0

    from gobby.storage.task_close_reviews import TaskCloseReviewStore
    from gobby.tasks.agentic_close_review import build_terminal_review_payload
    from gobby.tasks.close_review_delivery import terminal_review_delivery

    store = TaskCloseReviewStore(db)
    run_manager = LocalAgentRunManager(db)
    subscribers = CompletionSubscriberManager(db)
    reviews = await _run_db(runner, store.list_reconcilable)
    reconciled = 0
    for review in reviews:
        current = review
        run = (
            await _run_db(runner, run_manager.get, review.agent_run_id)
            if review.agent_run_id
            else None
        )
        if review.status == "launching":
            message = "Daemon restarted before the task-close validator launch was bound."
            payload = build_terminal_review_payload(review, status="error", message=message)
            current = (
                await _run_db(
                    runner,
                    store.finish,
                    review.id,
                    status="error",
                    result_payload=payload,
                    error=message,
                )
                or review
            )
            reconciled += 1
        elif review.active and run is None:
            message = "Persisted task-close validator run is missing after daemon restart."
            payload = build_terminal_review_payload(review, status="error", message=message)
            current = (
                await _run_db(
                    runner,
                    store.finish,
                    review.id,
                    status="error",
                    result_payload=payload,
                    error=message,
                )
                or review
            )
            reconciled += 1
        elif review.active and run is not None and run.status in TERMINAL_AGENT_RUN_STATUSES:
            await _run_db(runner, terminal_review_delivery, db, run.id)
            current = await _run_db(runner, store.get, review.id) or review
            reconciled += 1

        if current.agent_run_id and run is not None:
            await _run_db(
                runner,
                subscribers.add_completion_subscribers,
                current.agent_run_id,
                [current.caller_session_id],
            )
            continue
        if (
            current.terminal
            and current.delivered_at is None
            and current.result_payload is not None
            and callable(wake)
        ):
            try:
                outcome = await wake(
                    current.caller_session_id,
                    str(current.result_payload.get("message") or "Task-close review completed."),
                    current.result_payload,
                )
            except Exception:
                logger.warning(
                    "Task-close review startup delivery failed for session %s",
                    current.caller_session_id,
                    exc_info=True,
                )
                continue
            if wake_result_is_delivered(outcome):
                await _run_db(runner, store.mark_delivered, current.id)
                reconciled += 1
    return reconciled


async def _recover_agent_completion_subscribers_on_startup(runner: GobbyRunner) -> int:
    """Rehydrate active subscribers, then replay retained terminal notifications."""
    recovered = 0
    try:
        recovered += await _reconcile_task_close_reviews_on_startup(runner)
    except Exception:
        logger.warning("Failed to reconcile task-close reviews", exc_info=True)
    try:
        recovered += await _rehydrate_active_agent_completion_subscribers(runner)
    except Exception:
        logger.warning("Failed to rehydrate active agent completion subscribers", exc_info=True)
    try:
        recovered += await _cleanup_terminal_agent_completion_subscribers(runner)
    except Exception:
        logger.warning("Failed to redeliver terminal agent completion subscribers", exc_info=True)
    return recovered


_RUN_REPLAY_PAGE_SIZE = 500


async def _recover_agent_runs_after_restart(
    runner: GobbyRunner,
    *,
    include_fenced: bool = False,
) -> int:
    """Rehydrate completion events for active agent rows after daemon restart."""
    if runner.agent_runner is None or runner.completion_registry is None:
        return 0

    rehydrated = 0
    seen_ids: set[str] = set()
    offset = 0
    while True:
        batch = await _run_db(
            runner,
            runner.agent_runner.run_storage.list_active_for_machine,
            require_machine_id(),
            limit=_RUN_REPLAY_PAGE_SIZE,
            offset=offset,
        )
        if not batch:
            break
        for run in batch:
            if not include_fenced and is_reconciliation_pending(run):
                continue
            if run.id in seen_ids:
                continue
            seen_ids.add(run.id)
            if runner.completion_registry.is_registered(run.id):
                continue
            runner.completion_registry.register(
                run.id,
                subscribers=[],
                continuation_prompt=getattr(run, "continuation_prompt", None),
            )
            rehydrated += 1
        offset += len(batch)
        if len(batch) < _RUN_REPLAY_PAGE_SIZE:
            break

    return rehydrated


async def _reap_orphaned_srt_runners_on_startup(runner: GobbyRunner) -> int:
    """Reap managed SRT runners without an active agent-run row."""
    if runner.agent_runner is None:
        return 0
    active_runs = await _run_db(
        runner,
        _list_active_agent_runs_once,
        runner,
        include_fenced=True,
    )
    active_run_ids = {str(run.id) for run in active_runs}
    return await asyncio.to_thread(
        reap_orphaned_srt_runner_process_trees,
        active_run_ids,
    )


def _refresh_active_run_dispatch_mutex(runner: GobbyRunner, run: Any) -> bool:
    """Extend the dispatch mutex for a run that survived daemon restart."""
    task_id = getattr(run, "task_id", None)
    run_id = getattr(run, "id", None)
    if not task_id or not run_id:
        return False

    db = getattr(runner, "database", None)
    if db is None:
        run_storage = getattr(getattr(runner, "agent_runner", None), "run_storage", None)
        db = getattr(run_storage, "db", None)
    if db is None:
        return False

    try:
        return TaskDispatchMutexManager(db).acquire_mutex(
            str(task_id),
            holder="dispatcher",
            kind="heartbeat",
            ttl_seconds=600,
            run_id=str(run_id),
        )
    except Exception as e:
        logger.warning(
            "Failed to refresh dispatch mutex for recovered agent %s: %s",
            run_id,
            e,
        )
        return False


def _list_active_agent_runs_once(
    runner: GobbyRunner,
    *,
    include_fenced: bool = False,
) -> list[Any]:
    """List one de-duplicated view of active agent runs.

    ``include_fenced`` keeps reconciliation_pending runs in the view; shutdown
    preservation and the reclassification pass must see fenced runs too.
    """
    if runner.agent_runner is None:
        raise RuntimeError("Cannot list active agent runs: runner.agent_runner is not configured")
    run_storage = runner.agent_runner.run_storage
    active_runs: list[Any] = []
    seen_ids: set[str] = set()
    offset = 0
    while True:
        batch = run_storage.list_active_for_machine(
            require_machine_id(),
            limit=_RUN_REPLAY_PAGE_SIZE,
            offset=offset,
        )
        if not batch:
            break
        for run in batch:
            if not include_fenced and is_reconciliation_pending(run):
                continue
            run_id = str(getattr(run, "id", ""))
            if not run_id or run_id in seen_ids:
                continue
            seen_ids.add(run_id)
            active_runs.append(run)
        offset += len(batch)
        if len(batch) < _RUN_REPLAY_PAGE_SIZE:
            break
    return active_runs


async def _run_agent_hook_replay_barrier(
    runner: GobbyRunner,
    *,
    timeout_seconds: float = 5.0,
) -> bool:
    """Replay hook ingress and fence unresolved runs from restart classification."""
    agent_runner = getattr(runner, "agent_runner", None)
    http_server = getattr(runner, "http_server", None)
    app = getattr(http_server, "app", None)
    if app is None:
        return True

    from gobby.hooks.inbox import drain_hook_inbox_barrier

    result = await drain_hook_inbox_barrier(
        app,
        timeout_seconds=timeout_seconds,
    )
    if not result.timed_out:
        return True

    unresolved_run_ids = set(result.unresolved_run_ids)
    unresolved_session_ids = result.unresolved_session_ids
    session_manager = getattr(runner, "session_manager", None)
    if unresolved_session_ids and session_manager is None:
        logger.warning("Hook replay timed out while session services were unavailable")
        return False
    if session_manager is not None:
        for session_id in unresolved_session_ids:
            session = await _run_db(runner, session_manager.get, session_id)
            run_id = getattr(session, "agent_run_id", None)
            if isinstance(run_id, str) and run_id:
                unresolved_run_ids.add(run_id)

    if not unresolved_run_ids:
        logger.info(
            "Hook inbox replay timed out after replaying %d envelope(s); "
            "%d session identity/identities produced no agent runs",
            result.replayed,
            len(unresolved_session_ids),
        )
        return True
    if agent_runner is None:
        logger.warning("Hook replay timed out while agent services were unavailable")
        return False

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
            continue
        if run is None:
            missing_run_ids.add(run_id)
            continue
        if run.status in TERMINAL_AGENT_RUN_STATUSES:
            terminal_run_ids.add(run_id)
            continue
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
    if active_run_ids or unclassified_run_ids:
        logger.warning(
            "Agent hook replay barrier timed out with %d active fenced run(s) and "
            "%d unclassified run lookup(s)",
            len(active_run_ids),
            len(unclassified_run_ids),
        )
        return False
    return True


_MAX_NON_TASK_RESUME_FAILURES = 3


async def _retry_parked_non_task_resumes(runner: GobbyRunner) -> int:
    """Relaunch parked daemon-stop agents that no task dispatcher owns.

    Task-owned parked runs ride the dispatch tick; runs with no task would
    otherwise sit parked until the recovery-window reaper. Retries share the
    dispatcher's failure budget; exhausted candidates wait for the reaper.
    """
    config = runner.config_runtime.capture().snapshot.active
    if runner.agent_runner is None:
        return 0

    from gobby.agents.resume_executor import resume_agent_run
    from gobby.storage.agent_resume import increment_daemon_resume_failure_count

    run_storage = runner.agent_runner.run_storage
    try:
        candidates = await _run_db(
            runner,
            run_storage.list_parked_non_task_resume_candidates,
            machine_id=require_machine_id(),
        )
    except Exception:
        logger.warning("Failed to list parked non-task resume candidates", exc_info=True)
        return 0

    resumed = 0
    for run in candidates:
        metadata = run.resume_metadata_json or {}
        if not metadata:
            continue
        raw_count = metadata.get("daemon_stop_resume_failure_count")
        failure_count = raw_count if isinstance(raw_count, int) else 0
        if failure_count >= _MAX_NON_TASK_RESUME_FAILURES:
            continue
        try:
            result = await resume_agent_run(
                run,
                resume_metadata=metadata,
                runner=runner.agent_runner,
                session_manager=runner.session_manager,
                daemon_config=config,
                completion_registry=runner.completion_registry,
            )
        except Exception:
            logger.warning("Non-task parked resume raised for run %s", run.id, exc_info=True)
            await _run_db(
                runner, increment_daemon_resume_failure_count, runner.database, run_id=run.id
            )
            continue
        if result.success:
            resumed += 1
        else:
            logger.info(
                "Non-task parked resume failed for run %s: %s",
                run.id,
                result.error,
            )
            await _run_db(
                runner, increment_daemon_resume_failure_count, runner.database, run_id=run.id
            )
    return resumed
