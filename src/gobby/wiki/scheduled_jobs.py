from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from gobby.gwiki_gateway import GwikiCommandError, GwikiGateway, GwikiGatewayError
from gobby.scheduler.executor import CronHandler
from gobby.storage.cron import CronJobStorage, compute_next_run
from gobby.storage.cron_models import CronJob
from gobby.storage.hub.protocol import HubDatabase
from gobby.wiki.scheduled_jobs_history import (
    _health_history_output,
    _history_output,
    _librarian_history_output,
    _payload,
    _run_error,
    _status,
)
from gobby.wiki.scope_resolution import (
    ResolvedWikiScope,
    WikiScopeResolutionError,
    normalize_scope_identity,
    project_scope,
    resolve_scope_identity,
)
from gobby.wiki.update_coordinator import WikiUpdateCoordinator, written_cluster_paths

logger = logging.getLogger(__name__)

WIKI_REFRESH_INTERVAL_SECONDS = 60 * 60
WIKI_HEALTH_INTERVAL_SECONDS = 30 * 60
WIKI_AUDIT_INTERVAL_SECONDS = 24 * 60 * 60
WIKI_SYNC_SESSIONS_INTERVAL_SECONDS = 24 * 60 * 60
WIKI_UPKEEP_INTERVAL_SECONDS = 24 * 60 * 60
WIKI_LIBRARIAN_INTERVAL_SECONDS = 24 * 60 * 60
# Recaps run just after UTC midnight for the day that just ended; gwiki recap
# attributes sessions to UTC days, so the schedule stays in UTC.
WIKI_RECAP_SCHEDULE_CRON = "10 0 * * *"
WIKI_RECAP_TIMEOUT_SECONDS = 60 * 60
# Maintenance commands (librarian check sweeps, upkeep/recap synthesis) run
# far past the gateway's 30s interactive default; cron has no caller waiting.
WIKI_SCHEDULED_GATEWAY_TIMEOUT_SECONDS = 600.0
WIKI_LIBRARIAN_TASK_LABEL_PREFIX = "wiki-librarian"
_LIBRARIAN_DEDUP_LOOKUP_LIMIT = 20
_LIBRARIAN_FILED_TITLE_SAMPLE_SIZE = 10

RunSync = Callable[..., Awaitable[Any]]


async def _run_sync(
    run_sync: RunSync | None,
    operation: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    if run_sync is not None:
        return await run_sync(operation, *args, **kwargs)
    return await asyncio.to_thread(operation, *args, **kwargs)


class WikiGatewayProtocol(Protocol):
    async def refresh(
        self,
        *,
        source_ids: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]: ...

    async def health(self) -> dict[str, Any]: ...

    async def audit(self) -> dict[str, Any]: ...

    async def index(self) -> dict[str, Any]: ...

    async def sync_sessions(
        self,
        *,
        archive_dir: str | Path | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]: ...

    async def upkeep(self, *, dry_run: bool = False) -> dict[str, Any]: ...

    async def librarian(self) -> dict[str, Any]: ...

    async def recap(self, *, date: str | None = None) -> dict[str, Any]: ...


class CronRegistrationProtocol(Protocol):
    def register_handler(self, name: str, handler: CronHandler) -> None: ...


class LibrarianTaskManagerProtocol(Protocol):
    def list_tasks(
        self,
        *,
        project_id: str | None = ...,
        closed: bool | None = ...,
        title_like: str | None = ...,
        limit: int = ...,
    ) -> list[Any]: ...

    def create_task(
        self,
        project_id: str,
        title: str,
        description: str | None = None,
        *,
        labels: list[str] | None = None,
        category: str | None = None,
    ) -> object: ...


GatewayFactory = Callable[[ResolvedWikiScope], WikiGatewayProtocol]
WIKI_CRON_COMMANDS = ("refresh", "health", "audit")
WIKI_JOB_NAME_PREFIX = "gobby:wiki-"
WIKI_HANDLER_NAME_PREFIX = "wiki:"


def create_wiki_refresh_handler(
    *,
    gateway: WikiGatewayProtocol,
    coordinator: WikiUpdateCoordinator,
    scope: str,
) -> CronHandler:
    async def refresh_handler(job: CronJob) -> str:
        result = await gateway.refresh(source_ids=None)
        coordinated = await coordinator.handle_write_result(result)
        return _history_output(
            purpose="Refresh wiki sources",
            scope=scope,
            command="refresh",
            gwiki_result=coordinated,
            changed_paths=_refresh_changed_paths(coordinated),
        )

    return refresh_handler


def create_wiki_health_handler(
    *,
    gateway: WikiGatewayProtocol,
    scope: str,
) -> CronHandler:
    async def health_handler(job: CronJob) -> str:
        result = await gateway.health()
        return _health_history_output(
            purpose="Run wiki health checks",
            scope=scope,
            command="health",
            gwiki_result=result,
        )

    return health_handler


def create_wiki_audit_handler(
    *,
    gateway: WikiGatewayProtocol,
    coordinator: WikiUpdateCoordinator,
    scope: str,
) -> CronHandler:
    async def audit_handler(job: CronJob) -> str:
        result = await gateway.audit()
        coordinated = await coordinator.handle_write_result(result)
        return _history_output(
            purpose="Audit wiki content",
            scope=scope,
            command="audit",
            gwiki_result=coordinated,
            changed_paths=_changed_paths(coordinated),
        )

    return audit_handler


def create_wiki_sync_sessions_handler(
    *,
    gateway: WikiGatewayProtocol,
    coordinator: WikiUpdateCoordinator,
    scope: str,
) -> CronHandler:
    async def sync_sessions_handler(job: CronJob) -> str:
        result = await gateway.sync_sessions()
        coordinated = await coordinator.handle_write_result(result)
        return _history_output(
            purpose="Sync archived session transcripts",
            scope=scope,
            command="sync-sessions",
            gwiki_result=coordinated,
            changed_paths=_changed_paths(coordinated),
        )

    return sync_sessions_handler


def create_wiki_upkeep_handler(
    *,
    gateway: WikiGatewayProtocol,
    coordinator: WikiUpdateCoordinator,
    scope: str,
) -> CronHandler:
    async def upkeep_handler(job: CronJob) -> str:
        result = await gateway.upkeep()
        coordinated = await coordinator.handle_write_result(result)
        return _history_output(
            purpose="Drain pending wiki sources into concept pages",
            scope=scope,
            command="upkeep",
            gwiki_result=coordinated,
            changed_paths=_upkeep_changed_paths(coordinated),
        )

    return upkeep_handler


def create_wiki_librarian_handler(
    *,
    gateway: WikiGatewayProtocol,
    scope: str,
    task_manager: LibrarianTaskManagerProtocol | None,
    fallback_project_id: str,
) -> CronHandler:
    """Build the librarian cron handler.

    Librarian is read-only for the WikiUpdateCoordinator boundary: it emits
    proposals and writes only watcher-ignored ``meta/librarian/**`` artifacts,
    never canonical wiki content, so its result skips handle_write_result.
    Suggested tasks are filed into gobby-tasks instead.
    """

    async def librarian_handler(job: CronJob) -> str:
        result = await gateway.librarian()
        task_filing = await asyncio.to_thread(
            _file_librarian_tasks,
            task_manager=task_manager,
            scope=scope,
            fallback_project_id=fallback_project_id,
            suggested=_payload(result).get("suggested_tasks"),
        )
        return _librarian_history_output(
            purpose="File wiki librarian proposals as tasks",
            scope=scope,
            gwiki_result=result,
            task_filing=task_filing,
        )

    return librarian_handler


def create_wiki_recap_handler(
    *,
    gateway: WikiGatewayProtocol,
    coordinator: WikiUpdateCoordinator,
    scope: str,
) -> CronHandler:
    """Build the nightly recap cron handler.

    Session digests come from sync-sessions, whose 24h interval job carries no
    ordering guarantee against this schedule; a presync keeps the recap of the
    just-finished UTC day complete.
    """

    async def recap_handler(job: CronJob) -> str:
        try:
            presync = await coordinator.handle_write_result(await gateway.sync_sessions())
        except (GwikiCommandError, GwikiGatewayError) as exc:
            if isinstance(exc, GwikiCommandError):
                presync = exc.to_envelope()
            else:
                presync = {
                    "ok": False,
                    "command": "sync-sessions",
                    "status": "failed",
                    "payload": None,
                    "error": {"type": exc.__class__.__name__, "message": str(exc)},
                }
        result = await gateway.recap(date=_previous_utc_day())
        coordinated = await coordinator.handle_write_result(result)
        presync_payload = _payload(presync)
        presync_status = _status(presync, presync_payload)
        coordinated["presync"] = {
            "command": "sync-sessions",
            "status": presync_status,
        }
        presync_error = _run_error(
            presync, presync_payload, command="sync-sessions", status=presync_status
        )
        return _history_output(
            purpose="Write the nightly session recap page",
            scope=scope,
            command="recap",
            gwiki_result=coordinated,
            changed_paths=_changed_paths(coordinated),
            extra_error=f"presync sync-sessions: {presync_error}" if presync_error else None,
        )

    return recap_handler


async def register_wiki_cron_jobs(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistrationProtocol,
    project_id: str,
    db: HubDatabase | None = None,
    scopes: Iterable[str] | None = None,
    gateway_factory: GatewayFactory | None = None,
    task_manager: LibrarianTaskManagerProtocol | None = None,
) -> int:
    """Register wiki cron handlers and reconcile one cron row per scope and command."""
    return await register_wiki_cron_jobs_for_projects(
        cron_storage=cron_storage,
        cron_executor=cron_executor,
        project_scopes=((project_id, scopes),),
        db=db,
        gateway_factory=gateway_factory,
        task_manager=task_manager,
    )


async def register_wiki_cron_jobs_for_projects(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistrationProtocol,
    project_scopes: Iterable[tuple[str, Iterable[str] | None]],
    db: HubDatabase | None = None,
    gateway_factory: GatewayFactory | None = None,
    task_manager: LibrarianTaskManagerProtocol | None = None,
    run_sync: RunSync | None = None,
) -> int:
    """Register deduplicated wiki cron handlers for a paginated project stream."""
    if gateway_factory is None and db is None:
        raise ValueError(
            "register_wiki_cron_jobs_for_projects requires db when gateway_factory is not provided"
        )

    scope_projects: dict[str, str] = {}
    fallback_project_id = ""
    for project_id, scopes in project_scopes:
        if not fallback_project_id:
            fallback_project_id = project_id
        await _run_sync(
            run_sync,
            reconcile_stale_wiki_cron_scopes,
            cron_storage=cron_storage,
            project_id=project_id,
        )
        for scope in _configured_scopes(scopes, project_id):
            scope_projects.setdefault(scope, project_id)

    await _run_sync(run_sync, purge_legacy_wiki_research_jobs, cron_storage)
    if task_manager is None and db is not None:
        from gobby.storage.tasks import LocalTaskManager

        task_manager = LocalTaskManager(db)
    registered = 0
    for scope, project_id in sorted(scope_projects.items()):
        gateway = await _create_gateway(scope, db, gateway_factory)
        coordinator = WikiUpdateCoordinator(gateway)

        for command, purpose, interval, cron_expr, handler in _wiki_command_specs(
            gateway=gateway,
            coordinator=coordinator,
            scope=scope,
            task_manager=task_manager,
            fallback_project_id=project_id,
        ):
            handler_name = wiki_handler_name(command, scope)
            cron_executor.register_handler(handler_name, handler)
            await _run_sync(
                run_sync,
                _ensure_wiki_cron_job,
                cron_storage=cron_storage,
                project_id=project_id,
                command=command,
                scope=scope,
                handler_name=handler_name,
                purpose=purpose,
                interval_seconds=interval,
                cron_expr=cron_expr,
            )
            registered += 1

    registered += await _register_enabled_wiki_row_handlers(
        cron_storage=cron_storage,
        cron_executor=cron_executor,
        fallback_project_id=fallback_project_id,
        db=db,
        gateway_factory=gateway_factory,
        task_manager=task_manager,
        covered_scopes=set(scope_projects),
        run_sync=run_sync,
    )
    return registered


def _wiki_command_specs(
    *,
    gateway: WikiGatewayProtocol,
    coordinator: WikiUpdateCoordinator,
    scope: str,
    task_manager: LibrarianTaskManagerProtocol | None,
    fallback_project_id: str,
) -> tuple[tuple[str, str, int | None, str | None, CronHandler], ...]:
    """One (command, purpose, interval, cron_expr, handler) spec per wiki command."""
    return (
        (
            "refresh",
            "Scheduled wiki source refresh",
            WIKI_REFRESH_INTERVAL_SECONDS,
            None,
            create_wiki_refresh_handler(
                gateway=gateway,
                coordinator=coordinator,
                scope=scope,
            ),
        ),
        (
            "health",
            "Scheduled wiki health checks",
            WIKI_HEALTH_INTERVAL_SECONDS,
            None,
            create_wiki_health_handler(gateway=gateway, scope=scope),
        ),
        (
            "audit",
            "Scheduled wiki audit",
            WIKI_AUDIT_INTERVAL_SECONDS,
            None,
            create_wiki_audit_handler(
                gateway=gateway,
                coordinator=coordinator,
                scope=scope,
            ),
        ),
        (
            "sync-sessions",
            "Scheduled wiki session transcript sync",
            WIKI_SYNC_SESSIONS_INTERVAL_SECONDS,
            None,
            create_wiki_sync_sessions_handler(
                gateway=gateway,
                coordinator=coordinator,
                scope=scope,
            ),
        ),
        (
            "upkeep",
            "Scheduled wiki upkeep page drain",
            WIKI_UPKEEP_INTERVAL_SECONDS,
            None,
            create_wiki_upkeep_handler(
                gateway=gateway,
                coordinator=coordinator,
                scope=scope,
            ),
        ),
        (
            "librarian",
            "Scheduled wiki librarian proposals",
            WIKI_LIBRARIAN_INTERVAL_SECONDS,
            None,
            create_wiki_librarian_handler(
                gateway=gateway,
                scope=scope,
                task_manager=task_manager,
                fallback_project_id=fallback_project_id,
            ),
        ),
        (
            "recap",
            "Nightly wiki session recap",
            None,
            WIKI_RECAP_SCHEDULE_CRON,
            create_wiki_recap_handler(
                gateway=gateway,
                coordinator=coordinator,
                scope=scope,
            ),
        ),
    )


def _wiki_job_scope(job: CronJob) -> str | None:
    """Scope encoded in a wiki cron row's handler name or job name."""
    handler = job.action_config.get("handler") if isinstance(job.action_config, dict) else None
    for encoded, prefix in ((handler, WIKI_HANDLER_NAME_PREFIX), (job.name, WIKI_JOB_NAME_PREFIX)):
        if not isinstance(encoded, str) or not encoded.startswith(prefix):
            continue
        # "<prefix><command>:<scope>" — commands never contain ":", scopes may.
        _, _, scope = encoded.removeprefix(prefix).partition(":")
        if scope.strip():
            return normalize_scope_identity(scope)
    return None


async def _register_enabled_wiki_row_handlers(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistrationProtocol,
    fallback_project_id: str,
    db: HubDatabase | None,
    gateway_factory: GatewayFactory | None,
    task_manager: LibrarianTaskManagerProtocol | None,
    covered_scopes: set[str],
    run_sync: RunSync | None,
) -> int:
    """Register handlers for every enabled wiki system cron row, regardless of
    which project the daemon started in; park rows whose scope identity no
    longer resolves so they stop failing with "No handler registered"."""
    rows = await _run_sync(
        run_sync,
        cron_storage.list_system_jobs_by_name_prefix,
        WIKI_JOB_NAME_PREFIX,
        enabled=True,
    )
    rows_by_scope: dict[str, list[CronJob]] = {}
    for job in rows:
        scope = _wiki_job_scope(job)
        if scope is None or scope in covered_scopes:
            continue
        rows_by_scope.setdefault(scope, []).append(job)

    registered = 0
    for scope in sorted(rows_by_scope):
        scope_rows = rows_by_scope[scope]
        try:
            resolved = await resolve_scope_identity(
                db,
                scope,
                require_project_root=gateway_factory is None,
            )
            if (
                gateway_factory is None
                and resolved.project_root is not None
                and not resolved.project_root.exists()
            ):
                raise WikiScopeResolutionError(
                    f"Project root {resolved.project_root} for wiki scope {scope} no longer exists"
                )
        except WikiScopeResolutionError as exc:
            for job in scope_rows:
                await _run_sync(run_sync, cron_storage.park_system_job, job.id)
            logger.warning(
                "Parked %d enabled wiki cron row(s) for unresolvable scope %s: %s",
                len(scope_rows),
                scope,
                exc,
            )
            continue

        gateway = _gateway_for_resolved(resolved, gateway_factory)
        coordinator = WikiUpdateCoordinator(gateway)
        row_project_id = next(
            (job.project_id for job in scope_rows if job.project_id),
            fallback_project_id,
        )
        for command, _purpose, _interval, _cron_expr, handler in _wiki_command_specs(
            gateway=gateway,
            coordinator=coordinator,
            scope=scope,
            task_manager=task_manager,
            fallback_project_id=row_project_id,
        ):
            cron_executor.register_handler(wiki_handler_name(command, scope), handler)
            registered += 1
        for job in scope_rows:
            if job.next_run_at is None:
                await _run_sync(run_sync, cron_storage.wake_system_job, job.id)
    return registered


def wiki_handler_name(command: str, scope: str) -> str:
    return f"{WIKI_HANDLER_NAME_PREFIX}{command}:{scope}"


def wiki_job_name(command: str, scope: str) -> str:
    return f"{WIKI_JOB_NAME_PREFIX}{command}:{scope}"


async def _create_gateway(
    scope: str,
    db: HubDatabase | None,
    gateway_factory: GatewayFactory | None,
) -> WikiGatewayProtocol:
    if gateway_factory is None and db is None:
        raise ValueError("_create_gateway requires db when gateway_factory is not provided")
    resolved = await resolve_scope_identity(
        db,
        scope,
        require_project_root=gateway_factory is None,
    )
    return _gateway_for_resolved(resolved, gateway_factory)


def _gateway_for_resolved(
    resolved: ResolvedWikiScope,
    gateway_factory: GatewayFactory | None,
) -> WikiGatewayProtocol:
    if gateway_factory is not None:
        return gateway_factory(resolved)
    return GwikiGateway(
        project_root=resolved.project_root,
        topic=resolved.topic,
        timeout_seconds=WIKI_SCHEDULED_GATEWAY_TIMEOUT_SECONDS,
    )


def _ensure_wiki_cron_job(
    *,
    cron_storage: CronJobStorage,
    project_id: str,
    command: str,
    scope: str,
    handler_name: str,
    purpose: str,
    interval_seconds: int | None = None,
    cron_expr: str | None = None,
) -> None:
    if (interval_seconds is None) == (cron_expr is None):
        raise ValueError("provide exactly one of interval_seconds or cron_expr")
    schedule_type: Literal["cron", "interval"] = "cron" if cron_expr else "interval"
    job_name = wiki_job_name(command, scope)
    action_config: dict[str, Any] = {
        "handler": handler_name,
        "purpose": purpose,
        "scope": scope,
        "command": command,
    }
    if command == "recap":
        action_config["timeout_seconds"] = WIKI_RECAP_TIMEOUT_SECONDS
    description = f"{purpose} for wiki scope {scope}"
    existing = cron_storage.get_job_by_name(job_name)
    if existing is None:
        cron_storage.create_job(
            project_id=project_id,
            name=job_name,
            description=description,
            schedule_type=schedule_type,
            cron_expr=cron_expr,
            interval_seconds=interval_seconds,
            action_type="handler",
            action_config=action_config,
            enabled=True,
            is_system=True,
        )
        return

    if not existing.is_system:
        # Legacy non-system takeover: preserve the operator's enabled toggle,
        # recompute next_run_at for the bundled schedule, and mark the row
        # system so subsequent startups use the system reconcile path.
        candidate = replace(
            existing,
            schedule_type=schedule_type,
            cron_expr=cron_expr,
            interval_seconds=interval_seconds,
            run_at=None,
        )
        next_run_at = compute_next_run(candidate) if existing.enabled else None
        cron_storage.update_job(
            existing.id,
            description=description,
            schedule_type=schedule_type,
            cron_expr=cron_expr,
            interval_seconds=interval_seconds,
            run_at=None,
            action_type="handler",
            action_config=action_config,
            enabled=existing.enabled,
            next_run_at=next_run_at,
        )
        cron_storage.mark_as_system_job(existing.id)
        return

    reconciled = cron_storage.reconcile_system_job_definition(
        existing.id,
        action_type="handler",
        action_config=action_config,
        description=description,
        schedule_type=schedule_type,
        cron_expr=cron_expr,
        interval_seconds=interval_seconds,
    )
    if reconciled is not None and reconciled.enabled and reconciled.next_run_at is None:
        # A previous startup parked this row while its scope was
        # unresolvable; the scope is registering again, so wake it.
        cron_storage.wake_system_job(existing.id)


def purge_legacy_wiki_research_jobs(cron_storage: CronJobStorage) -> int:
    """Hard-delete every legacy wiki research cron row; `gwiki research` no
    longer exists, so system and operator rows alike are unrunnable."""
    return cron_storage.delete_retired_jobs_by_name_prefix(wiki_job_name("research", ""))


def _configured_scopes(scopes: Iterable[str] | None, project_id: str) -> list[str]:
    default_scope = project_scope(project_id) if project_id else None
    # None means "use the project default"; an explicit empty iterable means no scopes.
    values = list(scopes) if scopes is not None else ([default_scope] if default_scope else [])
    normalized = [normalize_scope_identity(scope) for scope in values if scope and scope.strip()]
    if normalized:
        return list(dict.fromkeys(normalized))
    if scopes is not None:
        return []
    return [default_scope] if default_scope else []


def reconcile_stale_wiki_cron_scopes(
    *,
    cron_storage: CronJobStorage,
    project_id: str,
) -> int:
    """Replace/disable legacy bare-project wiki cron rows with project:<id> rows."""
    legacy_scope = project_id.strip()
    if not legacy_scope or legacy_scope.startswith(("project:", "topic:")):
        return 0

    repaired = 0
    canonical_scope = project_scope(legacy_scope)
    for command in WIKI_CRON_COMMANDS:
        legacy = cron_storage.get_job_by_name(wiki_job_name(command, legacy_scope))
        if legacy is None or not legacy.is_system:
            # Non-system bare-scope rows are operator-owned; the system-only
            # identity reconcile would raise SystemRowProtected and abort
            # registration for every scope.
            continue

        canonical_name = wiki_job_name(command, canonical_scope)
        canonical = cron_storage.get_job_by_name(canonical_name)
        if canonical is None:
            cron_storage.reconcile_system_job_identity(legacy.id, name=canonical_name)
        else:
            cron_storage.reconcile_system_job_identity(
                legacy.id,
                enabled=False,
                next_run_at=None,
            )
        repaired += 1
    return repaired


def _refresh_changed_paths(result: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for entry in _payload(result).get("refreshed", []):
        if not isinstance(entry, dict) or not entry.get("changed"):
            continue
        raw_path = entry.get("raw_path")
        if isinstance(raw_path, str) and raw_path:
            paths.append(raw_path)
    return paths


def _changed_paths(result: dict[str, Any]) -> list[str]:
    payload = _payload(result)
    value = payload.get("changed_paths")
    paths = [path for path in value if isinstance(path, str)] if isinstance(value, list) else []
    page_path = payload.get("page_path")
    if isinstance(page_path, str) and page_path and page_path not in paths:
        paths.append(page_path)
    return paths


def _upkeep_changed_paths(result: dict[str, Any]) -> list[str]:
    """Page paths written by upkeep clusters; planned/failed clusters wrote nothing."""
    return written_cluster_paths(_payload(result).get("clusters"))


def _previous_utc_day() -> str:
    return (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")


def _file_librarian_tasks(
    *,
    task_manager: LibrarianTaskManagerProtocol | None,
    scope: str,
    fallback_project_id: str,
    suggested: Any,
) -> dict[str, Any]:
    entries = (
        [entry for entry in suggested if isinstance(entry, dict)]
        if isinstance(suggested, list)
        else []
    )
    if not entries:
        return {"status": "no_suggestions", "filed": 0, "deduplicated": 0}
    if task_manager is None:
        return {
            "status": "unavailable",
            "reason": "task manager not configured",
            "suggested": len(entries),
            "filed": 0,
            "deduplicated": 0,
        }

    project_id = _scope_project_id(scope, fallback_project_id)
    label = f"{WIKI_LIBRARIAN_TASK_LABEL_PREFIX}:{scope}"
    filed_titles: list[str] = []
    deduplicated = 0
    seen_titles: set[str] = set()
    for entry in entries:
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        title_key = title.casefold()
        if title_key in seen_titles or _has_open_task_titled(task_manager, project_id, title):
            deduplicated += 1
            continue
        seen_titles.add(title_key)
        task_manager.create_task(
            project_id,
            title,
            _librarian_task_description(entry),
            labels=[label],
            category="docs",
        )
        filed_titles.append(title)
    return {
        "status": "completed",
        "filed": len(filed_titles),
        "deduplicated": deduplicated,
        "titles": filed_titles[:_LIBRARIAN_FILED_TITLE_SAMPLE_SIZE],
    }


def _has_open_task_titled(
    task_manager: LibrarianTaskManagerProtocol,
    project_id: str,
    title: str,
) -> bool:
    candidates = task_manager.list_tasks(
        project_id=project_id,
        closed=False,
        title_like=title,
        limit=_LIBRARIAN_DEDUP_LOOKUP_LIMIT,
    )
    title_key = title.casefold()
    return any(
        str(getattr(candidate, "title", "")).strip().casefold() == title_key
        for candidate in candidates
    )


def _librarian_task_description(entry: dict[str, Any]) -> str | None:
    description = str(entry.get("description") or "").strip()
    paths = entry.get("paths")
    path_lines = (
        [str(path) for path in paths if isinstance(path, str) and path]
        if isinstance(paths, list)
        else []
    )
    if path_lines:
        listing = "\n".join(f"- {path}" for path in path_lines)
        affected = f"Affected paths:\n{listing}"
        return f"{description}\n\n{affected}" if description else affected
    return description or None


def _scope_project_id(scope: str, fallback_project_id: str) -> str:
    """Project scopes file tasks into their own project; topic scopes (for
    example ``topic:sessions`` for cross-project sessions) file into the
    registering project."""
    if scope.startswith("project:"):
        candidate = scope.removeprefix("project:").strip()
        if candidate:
            return candidate
    return fallback_project_id
