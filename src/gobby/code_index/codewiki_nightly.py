"""System cron registration for nightly codewiki refresh."""

from __future__ import annotations

import logging
import os
import signal
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
from gobby.code_index.gcode_gateway import GcodeCommandError, GcodeGateway
from gobby.config.wiki import WikiConfig, resolve_codewiki_scopes
from gobby.gwiki_gateway import GwikiGateway
from gobby.scheduler.executor import CronHandler
from gobby.shutdown_intent import read_active_shutdown_intent
from gobby.storage.cron import CronJobStorage, compute_next_run
from gobby.storage.cron_models import CronJob

logger = logging.getLogger(__name__)

CODEWIKI_NIGHTLY_CRON_EXPR = "0 3 * * *"
CODEWIKI_NIGHTLY_AI = "daemon"

# The first full run LLM-summarizes every core file (thousands of pages on a
# large repo); steady-state incremental runs finish in minutes via hash reuse.
# GcodeGateway's default rebuild timeout (120s) is sized for interactive
# projection rebuilds and would kill any real nightly generation pass.
CODEWIKI_NIGHTLY_GCODE_TIMEOUT_SECONDS = 8 * 60 * 60.0
CODEWIKI_NIGHTLY_GWIKI_TIMEOUT_SECONDS = 30 * 60.0


def nightly_refresh_service() -> CodewikiRefreshService:
    """Build a refresh service with gateways sized for unattended nightly runs."""
    return CodewikiRefreshService(
        gcode_gateway_factory=lambda: GcodeGateway(
            rebuild_timeout_seconds=CODEWIKI_NIGHTLY_GCODE_TIMEOUT_SECONDS,
        ),
        gwiki_gateway_factory=lambda root: GwikiGateway(
            project_root=root,
            timeout_seconds=CODEWIKI_NIGHTLY_GWIKI_TIMEOUT_SECONDS,
        ),
    )


class CronRegistrationProtocol(Protocol):
    def register_handler(self, name: str, handler: CronHandler) -> None: ...


def codewiki_nightly_job_name(project_id: str) -> str:
    return f"gobby:codewiki-nightly:{project_id}"


def codewiki_nightly_handler_name(project_id: str) -> str:
    return f"codewiki_nightly:{project_id}"


def _is_shutdown_interrupted_codewiki(exc: GcodeCommandError) -> bool:
    """True when gcode was SIGTERM'd because the daemon is shutting down.

    The nightly codewiki gcode subprocess is a non-agent child of the daemon.
    On a graceful restart/stop, child-process reaping sends SIGTERM to it,
    surfacing as ``gcode exited -15``. That is a benign interruption, not a
    real refresh failure, so it must not increment the cron job's consecutive
    failure counter — doing so drives a retry/backoff storm that keeps the
    vault from ever converging. Mirrors ``_is_noop_shutdown_prune`` in
    ``prune.py``; gated on a fresh shutdown-intent marker so genuine gcode
    SIGTERMs still surface as failures.
    """
    if exc.returncode != -signal.SIGTERM:
        return False
    return read_active_shutdown_intent() is not None


def create_codewiki_nightly_handler(
    *,
    project_id: str,
    root_path: Path,
    out_dir: Path,
    ai: str = CODEWIKI_NIGHTLY_AI,
    scopes: list[str] | None = None,
    refresh_service: CodewikiRefreshService | None = None,
) -> CronHandler:
    service = refresh_service or nightly_refresh_service()
    normalized_ai = normalize_codewiki_ai(ai)

    async def _handler(_job: CronJob) -> str:
        try:
            result = await service.refresh(
                CodewikiRefreshRequest(
                    root_path=str(root_path),
                    project_id=project_id,
                    out_dir=str(out_dir),
                    ai=normalized_ai,
                    scopes=scopes,
                )
            )
        except GcodeCommandError as exc:
            if _is_shutdown_interrupted_codewiki(exc):
                logger.info(
                    "codewiki nightly refresh for %s interrupted by daemon "
                    "shutdown; treating as benign no-op (not a failure)",
                    project_id,
                )
                return (
                    f"codewiki nightly refresh for {project_id} skipped: "
                    "daemon shutdown in progress"
                )
            raise
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
    project_name: str,
    repo_path: str | Path,
    wiki_config: WikiConfig,
    refresh_service: CodewikiRefreshService | None = None,
) -> int:
    """Register and reconcile the current project's nightly codewiki system job."""
    root = Path(repo_path).resolve(strict=False)
    service = refresh_service or nightly_refresh_service()
    out_dir = service.resolve_out_dir(root, None)
    enabled = bool(wiki_config.codewiki_nightly_enabled)
    cron_expr = wiki_config.codewiki_nightly_schedule_cron
    timezone = resolve_codewiki_nightly_timezone(wiki_config.codewiki_nightly_timezone)
    handler_name = codewiki_nightly_handler_name(project_id)
    job_name = codewiki_nightly_job_name(project_id)
    description = f"Nightly codewiki refresh for project {project_id}"
    scopes = resolve_codewiki_scopes(wiki_config, project_name)
    action_config = {
        "handler": handler_name,
        "project_id": project_id,
        "project_name": project_name,
        "root_path": str(root),
        "out_dir": str(out_dir),
        "ai": CODEWIKI_NIGHTLY_AI,
        "scopes": scopes,
    }

    cron_executor.register_handler(
        handler_name,
        create_codewiki_nightly_handler(
            project_id=project_id,
            root_path=root,
            out_dir=out_dir,
            ai=CODEWIKI_NIGHTLY_AI,
            scopes=scopes,
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
        logger.info(
            "Created codewiki nightly system cron job",
            extra={
                "job_name": job_name,
                "project_id": project_id,
                "cron_expr": cron_expr,
                "timezone": timezone,
                "enabled": enabled,
            },
        )
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
        logger.info(
            "Reconciled codewiki nightly system cron job",
            extra={
                "job_name": job_name,
                "project_id": project_id,
                "cron_expr": cron_expr,
                "timezone": timezone,
                "enabled": enabled,
            },
        )
    return 1


def register_codewiki_nightly_crons(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistrationProtocol,
    projects: Iterable[tuple[str, str, str | Path]],
    wiki_config: WikiConfig,
    refresh_service: CodewikiRefreshService | None = None,
) -> int:
    """Register the nightly codewiki cron for each ``(project_id, name, repo_path)``.

    Every project the memory dream judges per-project reads its own resolved
    vault's ``_meta/truth_digest.json``; a project whose codewiki is never
    refreshed is judged against a stale or absent digest. Registering one
    nightly refresh per memory-bearing repo keeps those digests fresh.

    Project IDs are de-duplicated and entries without a repo path are skipped.
    A single shared ``CodewikiRefreshService`` backs every registered handler.
    Returns the number of projects registered.
    """
    service = refresh_service or nightly_refresh_service()
    seen: set[str] = set()
    seen_repo_paths: set[str] = set()
    registered = 0
    for project_id, project_name, repo_path in projects:
        if not project_id or project_id in seen or not project_name or not repo_path:
            continue
        repo_key = str(Path(repo_path).resolve(strict=False))
        if repo_key in seen_repo_paths:
            continue
        seen.add(project_id)
        seen_repo_paths.add(repo_key)
        register_codewiki_nightly_cron(
            cron_storage=cron_storage,
            cron_executor=cron_executor,
            project_id=project_id,
            project_name=project_name,
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
    except (ValueError, ZoneInfoNotFoundError):
        return False
    return True
