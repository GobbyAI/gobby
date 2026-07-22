"""Daemon-owned reconciliation for project external issue providers."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

from gobby.github_triage.service import GitHubIssueTriageService
from gobby.storage.external_issue_sync import (
    ExternalIssueProvider,
    ExternalIssueSyncState,
    ExternalIssueSyncStatusStore,
)
from gobby.storage.github_triage import GitHubTriageConfig, GitHubTriageStore
from gobby.storage.projects import LocalProjectManager, Project
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.github_issue_sync import (
    GitHubIssueSyncService,
    GitHubRepositoryReadinessError,
)
from gobby.sync.linear import LinearSyncService
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)


class ExternalIssueSyncCoordinator:
    """Discover enabled projects and reconcile each provider independently."""

    def __init__(
        self,
        *,
        db: Any,
        mcp_manager: Any,
        task_manager: LocalTaskManager,
        project_manager: LocalProjectManager | None = None,
        memory_manager: Any | None = None,
        secret_store: Any | None = None,
        refresh_interval_seconds: float = 5.0,
        linear_interval_seconds: float = 300.0,
        linear_batch_interval_seconds: float = 5.0,
        linear_batch_size: int = 25,
        github_outbound_interval_seconds: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.mcp_manager = mcp_manager
        self.task_manager = task_manager
        self.project_manager = project_manager or LocalProjectManager(db)
        self.memory_manager = memory_manager
        self.secret_store = secret_store
        self.status_store = ExternalIssueSyncStatusStore(db)
        self.github_config_store = GitHubTriageStore(db)
        self.refresh_interval_seconds = refresh_interval_seconds
        self.linear_interval_seconds = linear_interval_seconds
        self.linear_batch_interval_seconds = linear_batch_interval_seconds
        self.linear_batch_size = linear_batch_size
        self.github_outbound_interval_seconds = github_outbound_interval_seconds
        self.monotonic = monotonic
        self._due: dict[tuple[str, str], float] = {}
        self._active: set[tuple[ExternalIssueProvider, str]] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._signatures: dict[str, tuple[Any, ...]] = {}
        self._generations: dict[tuple[ExternalIssueProvider, str], int] = {}
        self._provider_limits = {
            "linear": asyncio.Semaphore(2),
            "github": asyncio.Semaphore(2),
        }

    async def run(self, shutdown: asyncio.Event) -> None:
        """Refresh configuration until daemon shutdown, then drain active runs."""
        try:
            while not shutdown.is_set():
                await self.refresh()
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=self.refresh_interval_seconds)
                except TimeoutError:
                    pass
        finally:
            await self.wait_for_idle()

    async def refresh(self) -> None:
        """Discover current configuration and start due, non-overlapping runs."""
        projects = await asyncio.to_thread(self.project_manager.list)
        now = self.monotonic()
        live_ids = {project.id for project in projects}
        for project in projects:
            config = await asyncio.to_thread(
                self.github_config_store.get_config,
                project.id,
                project.github_repo,
            )
            signature = (
                project.linear_sync_enabled,
                project.linear_team_id,
                project.linear_project_id,
                config.sync_enabled,
                config.triage_enabled,
                config.webhook_enabled,
                config.repositories,
                config.reconcile_interval_seconds,
            )
            previous = self._signatures.get(project.id)
            changed = previous != signature
            self._signatures[project.id] = signature
            if changed:
                if previous is None or previous[:3] != signature[:3]:
                    self._advance_generation("linear", project.id)
                if previous is None or previous[3:] != signature[3:]:
                    self._advance_generation("github", project.id)
                self._prime_project(project, config, now)
                await self._record_disabled(project, config)
            self._schedule_due(project, config, now)

        for project_id in set(self._signatures) - live_ids:
            self._signatures.pop(project_id, None)
            self._generations.pop(("linear", project_id), None)
            self._generations.pop(("github", project_id), None)
            for due_key in [key for key in self._due if key[1] == project_id]:
                self._due.pop(due_key, None)

    async def wait_for_idle(self) -> None:
        """Wait for all runs already dispatched by refresh()."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    def _prime_project(self, project: Project, config: GitHubTriageConfig, now: float) -> None:
        if project.linear_sync_enabled:
            self._due[("linear", project.id)] = now
        else:
            self._due.pop(("linear", project.id), None)
        if config.sync_enabled or config.triage_enabled:
            self._due[("github_outbound", project.id)] = now
            self._due[("github_recovery", project.id)] = now
        else:
            self._due.pop(("github_outbound", project.id), None)
            self._due.pop(("github_recovery", project.id), None)

    async def _record_disabled(self, project: Project, config: GitHubTriageConfig) -> None:
        if not project.linear_sync_enabled:
            await self._write_status(project.id, "linear", "disabled", preserve_existing=True)
        if not (config.sync_enabled or config.triage_enabled):
            await self._write_status(project.id, "github", "disabled", preserve_existing=True)

    def _schedule_due(self, project: Project, config: GitHubTriageConfig, now: float) -> None:
        if project.linear_sync_enabled and self._is_due("linear", project.id, now):
            self._start(
                "linear",
                project.id,
                self._run_linear(project, self._generation("linear", project.id)),
            )

        github_key: tuple[ExternalIssueProvider, str] = ("github", project.id)
        if github_key in self._active or not (config.sync_enabled or config.triage_enabled):
            return
        outbound_due = config.sync_enabled and self._is_due("github_outbound", project.id, now)
        recovery_due = self._is_due("github_recovery", project.id, now)
        if outbound_due or recovery_due:
            self._start(
                "github",
                project.id,
                self._run_github(
                    project,
                    config,
                    outbound=outbound_due,
                    recovery=recovery_due,
                    generation=self._generation("github", project.id),
                ),
            )

    def _advance_generation(self, provider: ExternalIssueProvider, project_id: str) -> None:
        key = (provider, project_id)
        self._generations[key] = self._generations.get(key, 0) + 1

    def _generation(self, provider: ExternalIssueProvider, project_id: str) -> int:
        return self._generations.get((provider, project_id), 0)

    def _generation_is_current(
        self,
        provider: ExternalIssueProvider,
        project_id: str,
        generation: int,
    ) -> bool:
        return self._generation(provider, project_id) == generation

    async def _record_stale_run(
        self,
        provider: ExternalIssueProvider,
        project_id: str,
    ) -> None:
        signature = self._signatures.get(project_id)
        if signature is None:
            return
        enabled = bool(signature[0]) if provider == "linear" else bool(signature[3] or signature[4])
        await self._write_status(
            project_id,
            provider,
            "pending" if enabled else "disabled",
            preserve_existing=True,
        )

    def _is_due(self, operation: str, project_id: str, now: float) -> bool:
        return self._due.get((operation, project_id), math.inf) <= now

    def _start(
        self,
        provider: ExternalIssueProvider,
        project_id: str,
        operation: Any,
    ) -> None:
        key = (provider, project_id)
        if key in self._active:
            operation.close()
            return
        self._active.add(key)
        task = asyncio.create_task(
            self._guard_run(provider, project_id, operation),
            name=f"external-issue-sync:{provider}:{project_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guard_run(
        self,
        provider: ExternalIssueProvider,
        project_id: str,
        operation: Any,
    ) -> None:
        try:
            async with self._provider_limits[provider]:
                await operation
        except Exception:
            logger.exception("Unhandled %s issue sync failure for project %s", provider, project_id)
        finally:
            self._active.discard((provider, project_id))

    async def _run_linear(self, project: Project, generation: int | None = None) -> None:
        generation = self._generation("linear", project.id) if generation is None else generation
        if not self._generation_is_current("linear", project.id, generation):
            return
        attempt = utc_now()
        await self._write_status(
            project.id,
            "linear",
            "running",
            last_attempt_at=attempt,
            preserve_existing=True,
        )
        if not self._generation_is_current("linear", project.id, generation):
            await self._record_stale_run("linear", project.id)
            return
        service = LinearSyncService(
            mcp_manager=self.mcp_manager,
            task_manager=self.task_manager,
            project_id=project.id,
            linear_team_id=project.linear_team_id,
            linear_project_id=project.linear_project_id,
            project_manager=self.project_manager,
        )
        try:
            if not project.linear_team_id:
                raise ValueError("Linear sync is enabled without a Linear team binding")
            if not service.is_available():
                raise ValueError(service.get_unavailable_reason() or "Linear connector unavailable")
            created = await service.create_missing_issues(
                project.linear_team_id,
                limit=self.linear_batch_size,
            )
            if not self._generation_is_current("linear", project.id, generation):
                await self._record_stale_run("linear", project.id)
                return
            reconciliation = await service.sync_all(project.linear_team_id)
            if not self._generation_is_current("linear", project.id, generation):
                await self._record_stale_run("linear", project.id)
                return
            stats: dict[str, Any] = {
                "created": len(created),
                "reconciliation": reconciliation,
            }
            failures = _failure_count(stats)
            linked, pending = await asyncio.to_thread(
                self.status_store.counts, project.id, "linear"
            )
            no_progress = pending > 0 and not created and failures == 0
            if failures or no_progress:
                state: ExternalIssueSyncState = "degraded"
                error = (
                    "pending unlinked tasks remain after no-op reconciliation"
                    if no_progress
                    else f"Linear reconciliation reported {failures} failures"
                )
                await self._record_failure(
                    project.id,
                    "linear",
                    state,
                    attempt,
                    linked,
                    pending,
                    stats,
                    error,
                )
            else:
                state = "pending" if pending else "healthy"
                await self._write_status(
                    project.id,
                    "linear",
                    state,
                    linked=linked,
                    pending=pending,
                    last_attempt_at=attempt,
                    last_success_at=utc_now(),
                    statistics=stats,
                )
            self._due[("linear", project.id)] = self.monotonic() + (
                self.linear_batch_interval_seconds
                if pending or failures
                else self.linear_interval_seconds
            )
        except Exception as exc:
            if not self._generation_is_current("linear", project.id, generation):
                await self._record_stale_run("linear", project.id)
                return
            await self._record_exception(project.id, "linear", attempt, exc)
            self._due[("linear", project.id)] = self.monotonic() + self._retry_delay(exc)

    async def _run_github(
        self,
        project: Project,
        config: GitHubTriageConfig,
        *,
        outbound: bool,
        recovery: bool,
        generation: int | None = None,
    ) -> None:
        generation = self._generation("github", project.id) if generation is None else generation
        if not self._generation_is_current("github", project.id, generation):
            return
        attempt = utc_now()
        await self._write_status(
            project.id,
            "github",
            "running",
            last_attempt_at=attempt,
            preserve_existing=True,
        )
        if not self._generation_is_current("github", project.id, generation):
            await self._record_stale_run("github", project.id)
            return
        service = GitHubIssueSyncService(
            db=self.db,
            mcp_manager=self.mcp_manager,
            task_manager=self.task_manager,
            project_manager=self.project_manager,
        )
        try:
            repositories = await service.check_access(project, config)
            if not self._generation_is_current("github", project.id, generation):
                await self._record_stale_run("github", project.id)
                return
            stats: dict[str, Any] = {"repositories": list(repositories)}
            if outbound:
                stats["outbound"] = await service.push_linked_tasks(project.id)
                if not self._generation_is_current("github", project.id, generation):
                    await self._record_stale_run("github", project.id)
                    return
            if recovery:
                stats["recovery"] = await service.recover_project(project.id)
                if not self._generation_is_current("github", project.id, generation):
                    await self._record_stale_run("github", project.id)
                    return
                if config.triage_enabled:
                    triage = GitHubIssueTriageService(
                        db=self.db,
                        mcp_manager=self.mcp_manager,
                        task_manager=self.task_manager,
                        project_manager=self.project_manager,
                        memory_manager=self.memory_manager,
                        secret_store=self.secret_store,
                    )
                    stats["triage"] = await triage.reconcile_project_repos(project.id)
                    if not self._generation_is_current("github", project.id, generation):
                        await self._record_stale_run("github", project.id)
                        return
            failures = _failure_count(stats)
            linked, pending = await asyncio.to_thread(
                self.status_store.counts, project.id, "github"
            )
            if failures:
                await self._record_failure(
                    project.id,
                    "github",
                    "degraded",
                    attempt,
                    linked,
                    pending,
                    stats,
                    f"GitHub reconciliation reported {failures} failures",
                )
            else:
                await self._write_status(
                    project.id,
                    "github",
                    "pending" if pending else "healthy",
                    linked=linked,
                    pending=pending,
                    last_attempt_at=attempt,
                    last_success_at=utc_now(),
                    statistics=stats,
                )
            now = self.monotonic()
            if outbound:
                self._due[("github_outbound", project.id)] = (
                    now + self.github_outbound_interval_seconds
                )
            if recovery:
                self._due[("github_recovery", project.id)] = now + config.reconcile_interval_seconds
        except Exception as exc:
            if not self._generation_is_current("github", project.id, generation):
                await self._record_stale_run("github", project.id)
                return
            state: ExternalIssueSyncState = (
                "unready" if isinstance(exc, GitHubRepositoryReadinessError) else "degraded"
            )
            await self._record_exception(project.id, "github", attempt, exc, state=state)
            delay = self._retry_delay(exc)
            now = self.monotonic()
            if outbound:
                self._due[("github_outbound", project.id)] = now + delay
            if recovery:
                self._due[("github_recovery", project.id)] = now + delay

    async def _record_failure(
        self,
        project_id: str,
        provider: ExternalIssueProvider,
        state: ExternalIssueSyncState,
        attempt: Any,
        linked: int,
        pending: int,
        statistics: Mapping[str, Any],
        error: str,
    ) -> None:
        current = await asyncio.to_thread(self.status_store.get, project_id, provider)
        await self._write_status(
            project_id,
            provider,
            state,
            linked=linked,
            pending=pending,
            last_attempt_at=attempt,
            consecutive_failures=(current.consecutive_failures if current else 0) + 1,
            statistics=statistics,
            error=error,
        )

    async def _record_exception(
        self,
        project_id: str,
        provider: ExternalIssueProvider,
        attempt: Any,
        exc: Exception,
        *,
        state: ExternalIssueSyncState | None = None,
    ) -> None:
        linked, pending = await asyncio.to_thread(self.status_store.counts, project_id, provider)
        delay = self._retry_delay(exc)
        is_rate_limit = _is_rate_limit(exc)
        await self._record_failure(
            project_id,
            provider,
            "rate_limited" if is_rate_limit else state or "degraded",
            attempt,
            linked,
            pending,
            {},
            str(exc) or type(exc).__name__,
        )
        if is_rate_limit:
            current = await asyncio.to_thread(self.status_store.get, project_id, provider)
            if current:
                await self._write_status(
                    project_id,
                    provider,
                    "rate_limited",
                    linked=linked,
                    pending=pending,
                    last_attempt_at=attempt,
                    consecutive_failures=current.consecutive_failures,
                    retry_at=utc_now() + timedelta(seconds=delay),
                    error=current.last_error,
                )

    async def _write_status(
        self,
        project_id: str,
        provider: ExternalIssueProvider,
        state: ExternalIssueSyncState,
        *,
        linked: int | None = None,
        pending: int | None = None,
        last_attempt_at: Any = None,
        last_success_at: Any = None,
        consecutive_failures: int | None = None,
        retry_at: Any = None,
        statistics: Mapping[str, Any] | None = None,
        error: str | None = None,
        preserve_existing: bool = False,
    ) -> None:
        current = await asyncio.to_thread(self.status_store.get, project_id, provider)
        if linked is None or pending is None:
            linked, pending = await asyncio.to_thread(
                self.status_store.counts, project_id, provider
            )
        assert linked is not None and pending is not None
        await asyncio.to_thread(
            self.status_store.upsert,
            project_id=project_id,
            provider=provider,
            state=state,
            linked_count=linked,
            pending_count=pending,
            last_attempt_at=last_attempt_at,
            last_success_at=last_success_at or (current.last_success_at if current else None),
            consecutive_failures=(
                consecutive_failures
                if consecutive_failures is not None
                else (current.consecutive_failures if current else 0)
            ),
            retry_at=retry_at,
            last_statistics=(
                current.last_statistics
                if preserve_existing and statistics is None and current
                else statistics
            ),
            last_error=(
                current.last_error if preserve_existing and error is None and current else error
            ),
        )

    @staticmethod
    def _retry_delay(exc: Exception) -> float:
        for name in ("retry_after_seconds", "retry_after"):
            value = getattr(exc, name, None)
            if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
                return min(float(value), 300.0)
        return 30.0 if _is_rate_limit(exc) else 5.0


def _failure_count(value: Any) -> int:
    if isinstance(value, Mapping):
        total = sum(
            int(count)
            for key, count in value.items()
            if key in {"errors", "deferred"} and isinstance(count, int)
        )
        return total + sum(_failure_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_failure_count(item) for item in value)
    return 0


def _is_rate_limit(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "rate limit" in text or "status 429" in text or "http 429" in text
