"""System cron registration for nightly codewiki refresh."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gobby.code_index.codewiki_refresh import (
    CodewikiRefreshRequest,
    CodewikiRefreshService,
    normalize_codewiki_ai,
)
from gobby.config.wiki import WikiConfig
from gobby.scheduler.executor import CronHandler
from gobby.storage.cron import CronJobStorage, compute_next_run
from gobby.storage.cron_models import CronJob

logger = logging.getLogger(__name__)

CODEWIKI_NIGHTLY_CRON_EXPR = "0 3 * * *"
CODEWIKI_NIGHTLY_AI = "auto"


class CronRegistrationProtocol(Protocol):
    def register_handler(self, name: str, handler: CronHandler) -> None: ...


def codewiki_nightly_job_name(project_id: str) -> str:
    return f"gobby:codewiki-nightly:{project_id}"


def codewiki_nightly_handler_name(project_id: str) -> str:
    return f"codewiki_nightly:{project_id}"


def create_codewiki_nightly_handler(
    *,
    project_id: str,
    root_path: Path,
    out_dir: Path,
    ai: str = CODEWIKI_NIGHTLY_AI,
    refresh_service: CodewikiRefreshService | None = None,
) -> CronHandler:
    service = refresh_service or CodewikiRefreshService()
    normalized_ai = normalize_codewiki_ai(ai)

    async def _handler(_job: CronJob) -> str:
        result = await service.refresh(
            CodewikiRefreshRequest(
                root_path=str(root_path),
                project_id=project_id,
                out_dir=str(out_dir),
                ai=normalized_ai,
            )
        )
        return (
            f"codewiki nightly refresh completed for {project_id}: "
            f"{result.changed_count} changed doc(s)"
        )

    return _handler


def register_codewiki_nightly_cron(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistrationProtocol,
    project_id: str,
    repo_path: str | Path,
    wiki_config: WikiConfig,
    refresh_service: CodewikiRefreshService | None = None,
) -> int:
    """Register and reconcile the current project's nightly codewiki system job."""
    root = Path(repo_path).resolve(strict=False)
    service = refresh_service or CodewikiRefreshService()
    out_dir = service.resolve_out_dir(root, None)
    enabled = bool(wiki_config.codewiki_nightly_enabled)
    cron_expr = wiki_config.codewiki_nightly_schedule_cron
    timezone = resolve_codewiki_nightly_timezone(wiki_config.codewiki_nightly_timezone)
    handler_name = codewiki_nightly_handler_name(project_id)
    job_name = codewiki_nightly_job_name(project_id)
    description = f"Nightly codewiki refresh for project {project_id}"
    action_config = {
        "handler": handler_name,
        "project_id": project_id,
        "root_path": str(root),
        "out_dir": str(out_dir),
        "ai": CODEWIKI_NIGHTLY_AI,
    }

    cron_executor.register_handler(
        handler_name,
        create_codewiki_nightly_handler(
            project_id=project_id,
            root_path=root,
            out_dir=out_dir,
            ai=CODEWIKI_NIGHTLY_AI,
            refresh_service=service,
        ),
    )

    existing = cron_storage.get_job_by_name(job_name)
    if existing is None:
        cron_storage.create_job(
            project_id=project_id,
            name=job_name,
            description=description,
            schedule_type="cron",
            cron_expr=cron_expr,
            timezone=timezone,
            action_type="handler",
            action_config=action_config,
            enabled=enabled,
            is_system=True,
        )
        logger.info("Created system cron job: %s", job_name)
        return 1

    if not existing.is_system:
        cron_storage.mark_as_system_job(existing.id)

    repaired = cron_storage.reconcile_system_job_definition(
        existing.id,
        action_type="handler",
        action_config=action_config,
        description=description,
        schedule_type="cron",
        cron_expr=cron_expr,
        interval_seconds=None,
        run_at=None,
        timezone=timezone,
    )
    if repaired is not None:
        _reconcile_enabled_state(cron_storage, repaired, enabled)
    return 1


def register_codewiki_nightly_crons(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistrationProtocol,
    projects: Iterable[tuple[str, str | Path]],
    wiki_config: WikiConfig,
    refresh_service: CodewikiRefreshService | None = None,
) -> int:
    """Register the nightly codewiki cron for each ``(project_id, repo_path)``.

    Every project the memory dream judges per-project reads its own
    ``gobby-wiki/_meta/truth_digest.json``; a project whose codewiki is never
    refreshed is judged against a stale or absent digest. Registering one
    nightly refresh per memory-bearing repo keeps those digests fresh.

    Project IDs are de-duplicated and entries without a repo path are skipped.
    A single shared ``CodewikiRefreshService`` backs every registered handler.
    Returns the number of projects registered.
    """
    service = refresh_service or CodewikiRefreshService()
    seen: set[str] = set()
    registered = 0
    for project_id, repo_path in projects:
        if not project_id or project_id in seen or not repo_path:
            continue
        seen.add(project_id)
        register_codewiki_nightly_cron(
            cron_storage=cron_storage,
            cron_executor=cron_executor,
            project_id=project_id,
            repo_path=repo_path,
            wiki_config=wiki_config,
            refresh_service=service,
        )
        registered += 1
    return registered


def resolve_codewiki_nightly_timezone(configured_timezone: str | None = None) -> str:
    """Resolve the schedule timezone; execution timestamps remain UTC in storage."""
    configured = (configured_timezone or "").strip()
    if configured:
        return configured

    env_timezone = (os.environ.get("TZ") or "").strip()
    if env_timezone and not env_timezone.startswith(":") and _is_valid_timezone(env_timezone):
        return env_timezone

    try:
        target = Path("/etc/localtime").resolve(strict=True)
    except OSError:
        return "UTC"

    parts = target.parts
    if "zoneinfo" not in parts:
        return "UTC"
    zoneinfo_index = parts.index("zoneinfo")
    candidate = "/".join(parts[zoneinfo_index + 1 :])
    if candidate and _is_valid_timezone(candidate):
        return candidate
    return "UTC"


def _reconcile_enabled_state(
    cron_storage: CronJobStorage,
    job: CronJob,
    enabled: bool,
) -> None:
    if job.enabled == enabled:
        if enabled and job.next_run_at is None:
            cron_storage.wake_system_job(job.id)
        return

    if not enabled:
        cron_storage.reconcile_system_job_identity(job.id, enabled=False, next_run_at=None)
        return

    enabled_job = replace(job, enabled=True)
    next_run = compute_next_run(enabled_job)
    cron_storage.reconcile_system_job_identity(
        job.id,
        enabled=True,
        next_run_at=next_run.isoformat() if next_run else None,
    )


def _is_valid_timezone(value: str) -> bool:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return False
    return True
