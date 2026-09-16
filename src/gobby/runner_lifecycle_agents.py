"""Agent completion recovery and shared restart-replay primitives."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.agents.recovery_state import (
    is_daemon_stop_parked,
    is_reconciliation_pending,
)
from gobby.agents.sandbox_reaper import sweep_sandbox_run_roots
from gobby.agents.srt_process_cleanup import reap_orphaned_srt_runner_process_trees
from gobby.events.completion_registry import wake_result_is_delivered
from gobby.storage.agents import (
    TERMINAL_AGENT_RUN_STATUSES,
    LocalAgentRunManager,
)
from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.utils.datetime import utc_now
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner
    from gobby.storage.task_close_reviews import TaskCloseReview, TaskCloseReviewStore

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


async def _reconcile_task_close_reviews(
    runner: GobbyRunner,
    *,
    startup: bool = False,
) -> int:
    """Reconcile close reviews, including durable expiry and terminal delivery."""
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
        deadline = _close_review_deadline(review.close_arguments)
        if (
            review.active
            and deadline is not None
            and utc_now() >= deadline
            and (
                review.status == "launching"
                or (run is not None and run.status not in TERMINAL_AGENT_RUN_STATUSES)
            )
        ):
            message = "Task-close validator exceeded its durable deadline."
            get_cleanup = getattr(
                getattr(runner, "agent_lifecycle_monitor", None),
                "get_cleanup_agent",
                None,
            )
            cleanup_agent = get_cleanup() if callable(get_cleanup) else None
            if run is not None and run.status not in TERMINAL_AGENT_RUN_STATUSES:
                if callable(cleanup_agent):
                    await cleanup_agent(run, terminal_payload=message, is_timeout=True)
                else:
                    await _run_db(runner, run_manager.timeout, run.id, error=message)
            current = await _run_db(runner, store.get, review.id) or review
            if current.active:
                payload = build_terminal_review_payload(
                    current,
                    status="error",
                    message=message,
                    error_class="retryable_infrastructure",
                )
                current = (
                    await _run_db(
                        runner,
                        store.finish,
                        current.id,
                        status="error",
                        result_payload=payload,
                        error=message,
                    )
                    or current
                )
            reconciled += 1
        elif review.status == "launching" and startup:
            message = "Daemon restarted before the task-close validator launch was bound."
            payload = build_terminal_review_payload(
                review,
                status="error",
                message=message,
                error_class="retryable_infrastructure",
            )
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
        elif review.active and run is None and review.status != "launching":
            # A `finalizing` review whose run row was purged can still belong to
            # a task that did close. terminal_review_delivery reconstructs that
            # `closed` payload from the task alone, so consult it before
            # reporting a failure for a task that succeeded.
            reconstructed = (
                await _run_db(runner, terminal_review_delivery, db, review.agent_run_id)
                if review.status == "finalizing" and review.agent_run_id
                else None
            )
            if reconstructed is not None:
                current = await _run_db(runner, store.get, review.id) or review
            else:
                message = "Persisted task-close validator run is missing."
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

        if startup and current.status == "finalizing":
            swept = await _terminalize_orphaned_finalizing(runner, db, store, current)
            if swept is not None:
                if swept.status != "finalizing":
                    reconciled += 1
                current = swept

        if startup and current.active and current.agent_run_id and run is not None:
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
                if current.agent_run_id:
                    await _run_db(
                        runner,
                        subscribers.remove_completion_subscribers,
                        current.agent_run_id,
                        session_ids=[current.caller_session_id],
                    )
                reconciled += 1
    return reconciled


async def _terminalize_orphaned_finalizing(
    runner: GobbyRunner,
    db: Any,
    store: TaskCloseReviewStore,
    review: TaskCloseReview,
) -> TaskCloseReview | None:
    """Release a `finalizing` review the daemon abandoned mid-submission.

    Startup-only by contract, and that scope is load-bearing rather than
    cautious. `claim_finalizing` holds `finalizing` across `commit_close`'s git
    subprocesses, so on the periodic tick this status is reachable while a real
    `submit_close_review` is still running and the reconciler would win the
    write and then contradict the caller. No in-process submit survives a
    restart, so only at daemon start is every `finalizing` row provably
    orphaned. Without this the row holds `uq_task_close_reviews_active_task`
    forever and the task can never be closed again.

    A verdict is never reapplied here: `commit_close` resolves project context
    from the process cwd, which in the daemon loop is the daemon's own, so a
    reconciler-driven reapply could evaluate a different commit set than the
    validator reviewed. A fresh `close_task` re-derives every input.
    """
    from gobby.tasks.agentic_close_review import (
        CLOSE_REVIEW_DAEMON_STOP_RETRY_SECONDS,
        build_terminal_review_payload,
    )
    from gobby.tasks.close_review_delivery import terminal_review_delivery

    if review.agent_run_id:
        # The task may have closed before the interruption; this path rebuilds
        # the `closed` payload from the task itself and needs no verdict.
        await _run_db(runner, terminal_review_delivery, db, review.agent_run_id)
        review = await _run_db(runner, store.get, review.id) or review
        if review.status != "finalizing":
            return review
    # The captured submission distinguishes a verdict that reached the daemon
    # and died inside finalization from one that was never sent (#22404); the
    # terminal payload replaces it, so record the distinction before the write.
    logger.warning(
        "Terminalizing task-close review %s orphaned in finalizing by a daemon restart "
        "(submitted verdict captured: %s)",
        review.id,
        review.result_payload is not None,
    )
    message = "Daemon restarted while the task-close verdict was being finalized."
    payload = build_terminal_review_payload(
        review,
        status="error",
        message=message,
        error_class="retryable_infrastructure",
        retry_seconds=CLOSE_REVIEW_DAEMON_STOP_RETRY_SECONDS,
    )
    swept: TaskCloseReview | None = await _run_db(
        runner,
        store.finish_orphaned_finalizing,
        review.id,
        expected_updated_at=review.updated_at,
        result_payload=payload,
        error=message,
    )
    return swept


def _close_review_deadline(close_arguments: dict[str, Any]) -> datetime | None:
    raw = close_arguments.get("_review_deadline_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def _reconcile_task_close_reviews_on_startup(runner: GobbyRunner) -> int:
    """Apply startup-only orphan handling in addition to normal reconciliation."""
    return await _reconcile_task_close_reviews(runner, startup=True)


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
    run_ids: frozenset[str] | None = None,
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
            if run_ids is not None and str(run.id) not in run_ids:
                continue
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
    """Reap managed SRT processes and schedule old run-root cleanup."""
    if runner.agent_runner is None:
        return 0
    active_runs = await _run_db(
        runner,
        _list_active_agent_runs_once,
        runner,
        include_fenced=True,
    )
    active_run_ids = {str(run.id) for run in active_runs}
    reaped_processes = await asyncio.to_thread(
        reap_orphaned_srt_runner_process_trees,
        active_run_ids,
    )
    runner._sandbox_run_root_sweep_task = asyncio.create_task(
        _sweep_sandbox_run_roots_on_startup(runner),
        name="sandbox-run-root-sweep",
    )
    return reaped_processes


async def _sweep_sandbox_run_roots_on_startup(runner: GobbyRunner) -> None:
    """Sweep old run roots against active state read when the task starts."""
    if runner.agent_runner is None:
        return
    try:
        active_runs = await _run_db(
            runner,
            _list_active_agent_runs_once,
            runner,
            include_fenced=True,
        )
        await sweep_sandbox_run_roots({str(run.id) for run in active_runs})
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Sandbox run-root sweep failed during startup")


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
        mutexes = TaskDispatchMutexManager(db)
        mutex = mutexes.get_mutex(str(task_id))
        if mutex is not None:
            if mutex.run_id is not None and str(mutex.run_id) == str(run_id) and mutex.lease_holder:
                return mutexes.refresh_mutex_for_run(
                    str(task_id),
                    str(run_id),
                    lease_holder=mutex.lease_holder,
                    ttl_seconds=600,
                )
            return False
        return mutexes.acquire_mutex(
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

    horizon = getattr(runner, "http_bound_at_ms", None)
    result = await drain_hook_inbox_barrier(
        app,
        timeout_seconds=timeout_seconds,
        restart_horizon_ms=horizon if isinstance(horizon, int) else None,
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
            "%d session identity/identities produced no agent runs "
            "(residue_hooks=%d live_hooks=%d receipts=%d)",
            result.replayed,
            len(unresolved_session_ids),
            result.residue_hook_count,
            result.live_hook_count,
            result.receipt_count,
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
            "%d unclassified run lookup(s) (runs=%s residue_hooks=%d live_hooks=%d "
            "receipts=%d)",
            len(active_run_ids),
            len(unclassified_run_ids),
            ",".join(sorted(active_run_ids)) or "-",
            result.residue_hook_count,
            result.live_hook_count,
            result.receipt_count,
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
