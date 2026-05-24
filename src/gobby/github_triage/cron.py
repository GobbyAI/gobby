"""Cron registration for GitHub issue triage reconciliation."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from gobby.github_triage.service import create_github_triage_handler
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.github_triage import GitHubTriageStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager, Project
from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)

GITHUB_TRIAGE_CRON_DESCRIPTION = "Webhook recovery scan for GitHub issue triage"
GITHUB_TRIAGE_CRON_HANDLER_PREFIX = "github_triage.reconcile"
GITHUB_TRIAGE_CRON_JOB_PREFIX = "gobby:github-triage"
CronHandler = Callable[[CronJob], Awaitable[str]]


class CronRegistrationProtocol(Protocol):
    def register_handler(self, name: str, handler: CronHandler) -> None:
        """Register a cron handler by name."""
        ...


class GitHubMCPCallProtocol(Protocol):
    def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> object:
        """Call a GitHub MCP tool."""
        ...


class TriageMemoryProtocol(Protocol):
    @property
    def vector_store(self) -> object | None:
        """Return the configured vector store, if any."""
        ...

    @property
    def embed_fn(self) -> object | None:
        """Return the configured embedding function, if any."""
        ...


class SecretResolverProtocol(Protocol):
    def resolve(self, value: str) -> str:
        """Resolve a secret reference."""
        ...


def github_triage_handler_name(project_id: str) -> str:
    """Return the project-specific cron handler name."""
    return f"{GITHUB_TRIAGE_CRON_HANDLER_PREFIX}:{project_id}"


def github_triage_job_name(project_id: str) -> str:
    """Return the project-specific system cron job name."""
    return f"{GITHUB_TRIAGE_CRON_JOB_PREFIX}:{project_id}"


def register_github_triage_cron(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistrationProtocol,
    db: HubDatabase,
    mcp_manager: GitHubMCPCallProtocol | None,
    task_manager: LocalTaskManager,
    project_manager: LocalProjectManager | None = None,
    memory_manager: TriageMemoryProtocol | None = None,
    secret_store: SecretResolverProtocol | None = None,
    project_id: str | None = None,
) -> int:
    """Register reconciliation handlers and reconcile system cron rows.

    The webhook path is primary. These project-scoped interval jobs are a
    recovery path for missed deliveries and can be disabled by project config.
    """
    projects = _projects_for_registration(project_manager or LocalProjectManager(db), project_id)
    store = GitHubTriageStore(db)
    registered = 0

    for project in projects:
        config = store.get_config(project.id, fallback_repo=project.github_repo)
        handler_name = github_triage_handler_name(project.id)
        job_name = github_triage_job_name(project.id)
        existing = cron_storage.get_job_by_name(job_name)

        if not config.enabled or not config.repositories_with_fallback(project.github_repo):
            if existing and existing.enabled:
                cron_storage.update_job(existing.id, enabled=False)
                cron_storage.update_system_job_bookkeeping(existing.id, next_run_at=None)
                logger.info("Disabled system cron job: %s", job_name)
            continue

        handler = create_github_triage_handler(
            db=db,
            mcp_manager=mcp_manager,
            task_manager=task_manager,
            memory_manager=memory_manager,
            secret_store=secret_store,
        )
        cron_executor.register_handler(handler_name, handler)
        registered += 1

        if existing is None:
            cron_storage.create_job(
                project_id=project.id,
                name=job_name,
                description=GITHUB_TRIAGE_CRON_DESCRIPTION,
                schedule_type="interval",
                interval_seconds=config.reconcile_interval_seconds,
                action_type="handler",
                action_config={"handler": handler_name},
                enabled=True,
                is_system=True,
            )
            logger.info("Created system cron job: %s", job_name)
            continue

        if not existing.is_system:
            with cron_storage.db.transaction() as conn:
                conn.execute(
                    "UPDATE cron_jobs SET is_system = 1 WHERE id = ?",
                    (existing.id,),
                )

        repaired = cron_storage.reconcile_system_job_definition(
            existing.id,
            action_type="handler",
            action_config={"handler": handler_name},
        )
        if repaired is None:
            raise RuntimeError(f"GitHub triage cron row disappeared: {existing.id}")

        if (
            repaired.schedule_type != "interval"
            or repaired.interval_seconds != config.reconcile_interval_seconds
            or not repaired.enabled
        ):
            cron_storage.update_job(
                repaired.id,
                schedule_type="interval",
                cron_expr=None,
                interval_seconds=config.reconcile_interval_seconds,
                run_at=None,
                enabled=True,
            )

    return registered


def _projects_for_registration(
    project_manager: LocalProjectManager,
    project_id: str | None,
) -> list[Project]:
    if project_id:
        project = project_manager.get(project_id)
        return [project] if project and not project.deleted_at else []
    return project_manager.list()
