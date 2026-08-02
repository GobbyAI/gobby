"""Lifecycle-managed commit auto-linking for session-end hooks."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.commits import AutoLinkResult, auto_link_commits
from gobby.telemetry.instruments import dec_gauge, inc_counter, inc_gauge
from gobby.utils.datetime import datetime_to_required_iso

_METRIC_ATTRIBUTES = {"component": "session_end_commit_auto_link"}


@dataclass(frozen=True, slots=True)
class SessionEndAutoLinkJob:
    """Immutable inputs captured before the session-end hook returns."""

    session_id: str
    project_id: str
    created_at: datetime
    cwd: str | None


class SessionEndAutoLinkWorker:
    """Own session-end auto-link jobs through completion and daemon shutdown."""

    def __init__(
        self,
        *,
        database: HubDatabase,
        task_manager: LocalTaskManager,
        logger: logging.Logger,
    ) -> None:
        self._database = database
        self._task_manager = task_manager
        self._logger = logger
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="gobby-session-end-auto-link",
        )
        self._lock = threading.Lock()
        self._futures: set[Future[AutoLinkResult]] = set()
        self._closed = False

    def submit(self, job: SessionEndAutoLinkJob) -> None:
        """Accept a job for background execution while the worker is open."""
        with self._lock:
            if self._closed:
                raise RuntimeError("session-end auto-link worker is closed")
            future = self._executor.submit(self._run, job)
            self._futures.add(future)

        inc_counter("background_tasks_total", attributes=_METRIC_ATTRIBUTES)
        inc_gauge("background_tasks_active", attributes=_METRIC_ATTRIBUTES)
        future.add_done_callback(lambda done: self._on_done(job, done))

    def shutdown(self) -> None:
        """Stop accepting jobs and drain every accepted job before returning."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _run(self, job: SessionEndAutoLinkJob) -> AutoLinkResult:
        project = LocalProjectManager(self._database).get(job.project_id)
        if project is None:
            raise ValueError(f"Project {job.project_id} not found")

        result = auto_link_commits(
            task_manager=self._task_manager,
            since=datetime_to_required_iso(job.created_at),
            cwd=job.cwd,
            project_name=project.name,
            project_id=job.project_id,
        )
        if result.total_linked > 0:
            self._logger.info(
                "SESSION_END: auto-linked %s commits for session %s project %s to tasks: %s",
                result.total_linked,
                job.session_id,
                job.project_id,
                list(result.linked_tasks),
            )
        return result

    def _on_done(self, job: SessionEndAutoLinkJob, future: Future[AutoLinkResult]) -> None:
        with self._lock:
            self._futures.discard(future)
        dec_gauge("background_tasks_active", attributes=_METRIC_ATTRIBUTES)

        exception = future.exception()
        if exception is None:
            inc_counter("background_tasks_completed_total", attributes=_METRIC_ATTRIBUTES)
            return

        inc_counter("background_tasks_failed_total", attributes=_METRIC_ATTRIBUTES)
        self._logger.error(
            "SESSION_END: commit auto-link failed for session %s project %s: %s",
            job.session_id,
            job.project_id,
            exception,
            exc_info=(type(exception), exception, exception.__traceback__),
        )
