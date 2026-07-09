from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from gobby.gwiki_gateway import GwikiCommandError, GwikiGateway, GwikiGatewayError
from gobby.scheduler.executor import CronHandler
from gobby.storage.cron import CronJobStorage, compute_next_run
from gobby.storage.cron_models import CronJob
from gobby.storage.hub.protocol import HubDatabase
from gobby.wiki.scope_resolution import (
    ResolvedWikiScope,
    normalize_scope_identity,
    project_scope,
    resolve_scope_identity,
)
from gobby.wiki.update_coordinator import WikiUpdateCoordinator, written_cluster_paths

WIKI_REFRESH_INTERVAL_SECONDS = 60 * 60
WIKI_HEALTH_INTERVAL_SECONDS = 30 * 60
WIKI_AUDIT_INTERVAL_SECONDS = 24 * 60 * 60
WIKI_SYNC_SESSIONS_INTERVAL_SECONDS = 24 * 60 * 60
WIKI_UPKEEP_INTERVAL_SECONDS = 24 * 60 * 60
WIKI_LIBRARIAN_INTERVAL_SECONDS = 24 * 60 * 60
# Recaps run just after UTC midnight for the day that just ended; gwiki recap
# attributes sessions to UTC days, so the schedule stays in UTC.
WIKI_RECAP_SCHEDULE_CRON = "10 0 * * *"
# Maintenance commands (librarian check sweeps, upkeep/recap synthesis) run
# far past the gateway's 30s interactive default; cron has no caller waiting.
WIKI_SCHEDULED_GATEWAY_TIMEOUT_SECONDS = 600.0
# Gwiki statuses that must record a failed cron run so consecutive_failures
# and backoff engage; "degraded" covers gwiki timeout envelopes, which report
# ok:false with status "degraded" instead of "failed".
_FAILED_RUN_STATUSES = frozenset({"failed", "failure", "error", "timeout", "degraded"})
WIKI_HEALTH_HISTORY_SAMPLE_SIZE = 10
WIKI_LIBRARIAN_TASK_LABEL_PREFIX = "wiki-librarian"
_LIBRARIAN_DEDUP_LOOKUP_LIMIT = 20
_LIBRARIAN_FILED_TITLE_SAMPLE_SIZE = 10

_HEALTH_HISTORY_LIST_FIELDS = (
    "broken_links",
    "stale_pages",
    "stale_citations",
    "uncited_sources",
    "uncompiled_sources",
    "duplicate_concepts",
)


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
    if gateway_factory is None and db is None:
        raise ValueError("register_wiki_cron_jobs requires db when gateway_factory is not provided")
    reconcile_stale_wiki_cron_scopes(cron_storage=cron_storage, project_id=project_id)
    purge_legacy_wiki_research_jobs(cron_storage)
    if task_manager is None and db is not None:
        from gobby.storage.tasks import LocalTaskManager

        task_manager = LocalTaskManager(db)
    registered = 0
    for scope in _configured_scopes(scopes, project_id):
        gateway = await _create_gateway(scope, db, gateway_factory)
        coordinator = WikiUpdateCoordinator(gateway)

        for command, purpose, interval, cron_expr, handler in (
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
                    fallback_project_id=project_id,
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
        ):
            handler_name = wiki_handler_name(command, scope)
            cron_executor.register_handler(handler_name, handler)
            _ensure_wiki_cron_job(
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

    return registered


def configured_wiki_cron_scopes(config: object | None, project_id: str) -> list[str]:
    if config is None:
        return [project_scope(project_id)]

    wiki_config = getattr(config, "wiki", None)
    scopes = _scopes_from_config_value(getattr(wiki_config, "scheduled_scopes", None))
    if scopes:
        return scopes

    scopes = _scopes_from_config_value(getattr(config, "wiki_scheduled_scopes", None))
    return scopes or [project_scope(project_id)]


def wiki_handler_name(command: str, scope: str) -> str:
    return f"wiki:{command}:{scope}"


def wiki_job_name(command: str, scope: str) -> str:
    return f"gobby:wiki-{command}:{scope}"


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
    action_config = {
        "handler": handler_name,
        "purpose": purpose,
        "scope": scope,
        "command": command,
    }
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

    if existing.is_system:
        cron_storage.reconcile_system_job_definition(
            existing.id,
            action_type="handler",
            action_config=action_config,
            description=description,
            schedule_type=schedule_type,
            cron_expr=cron_expr,
            interval_seconds=interval_seconds,
        )


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


def _history_output(
    *,
    purpose: str,
    scope: str,
    command: str,
    gwiki_result: dict[str, Any],
    changed_paths: list[str] | None = None,
    extra_error: str | None = None,
) -> str:
    payload = _payload(gwiki_result)
    status = _status(gwiki_result, payload)
    error = _run_error(gwiki_result, payload, command=command, status=status)
    if extra_error:
        error = f"{error}; {extra_error}" if error else extra_error
    return _history_output_json(
        purpose=purpose,
        scope=scope,
        command=command,
        status=status,
        error=error,
        result=_visible_result(gwiki_result, payload),
        changed_paths=changed_paths,
    )


def _librarian_history_output(
    *,
    purpose: str,
    scope: str,
    gwiki_result: dict[str, Any],
    task_filing: dict[str, Any],
) -> str:
    """Compact librarian history: full check item lists and patch diff bodies
    overflow the cron run output budget (see _truncate in the executor)."""
    payload = _payload(gwiki_result)
    result = _visible_health_result(gwiki_result, _compact_librarian_payload(payload))
    result["task_filing"] = task_filing
    status = _status(gwiki_result, payload)
    return _history_output_json(
        purpose=purpose,
        scope=scope,
        command="librarian",
        status=status,
        error=_run_error(gwiki_result, payload, command="librarian", status=status),
        result=result,
    )


def _compact_librarian_payload(
    payload: dict[str, Any],
    *,
    sample_size: int = WIKI_HEALTH_HISTORY_SAMPLE_SIZE,
) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "checks" and isinstance(value, list):
            compact[key] = [_compact_librarian_check(check, sample_size) for check in value]
        elif key == "suggested_tasks" and isinstance(value, list):
            compact["suggested_tasks_count"] = len(value)
        elif key == "suggested_patch_diffs" and isinstance(value, list):
            compact["suggested_patch_diffs_count"] = len(value)
            compact["suggested_patch_diffs_sample"] = [
                {"path": diff.get("path"), "summary": diff.get("summary")}
                for diff in value[:sample_size]
                if isinstance(diff, dict)
            ]
        elif key in ("artifacts", "degradation", "dependency_classification", "error"):
            compact[key] = value
        elif _is_json_scalar(value):
            compact[key] = value
    return compact


def _compact_librarian_check(check: Any, sample_size: int) -> dict[str, Any]:
    if not isinstance(check, dict):
        return {"items_count": 0}
    compact = {key: value for key, value in check.items() if _is_json_scalar(value)}
    items = check.get("items")
    if isinstance(items, list):
        compact["items_count"] = len(items)
        compact["items_sample"] = items[:sample_size]
    return compact


def _health_history_output(
    *,
    purpose: str,
    scope: str,
    command: str,
    gwiki_result: dict[str, Any],
) -> str:
    payload = _payload(gwiki_result)
    status = _status(gwiki_result, payload)
    return _history_output_json(
        purpose=purpose,
        scope=scope,
        command=command,
        status=status,
        error=_run_error(gwiki_result, payload, command=command, status=status),
        result=_visible_health_result(gwiki_result, _compact_health_payload(payload)),
    )


def _history_output_json(
    *,
    purpose: str,
    scope: str,
    command: str,
    status: str,
    result: dict[str, Any],
    error: str | None = None,
    changed_paths: list[str] | None = None,
) -> str:
    # The cron executor parses JSON handler output and coerces top-level
    # ok/error into the run outcome, so failed and degraded gwiki results
    # record failed runs instead of silently completing.
    output: dict[str, Any] = {
        "purpose": purpose,
        "scope": scope,
        "command": command,
        "status": status,
        "ok": error is None,
        "result": result,
    }
    if error is not None:
        output["error"] = error
    if changed_paths is not None:
        output["changed_paths"] = changed_paths
    return json.dumps(output, sort_keys=True)


def _visible_result(result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    visible = dict(payload)
    visible["gwiki"] = {
        "ok": result.get("ok"),
        "command": result.get("command"),
        "payload": payload,
        "stderr": result.get("stderr", ""),
    }
    for passthrough in ("index_handoff", "task_filing", "presync"):
        if passthrough in result:
            visible[passthrough] = result[passthrough]
    return visible


def _visible_health_result(result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    visible = dict(payload)
    visible["gwiki"] = {
        "ok": result.get("ok"),
        "command": result.get("command"),
        "stderr": result.get("stderr", ""),
    }
    return visible


def _compact_health_payload(
    payload: dict[str, Any],
    *,
    sample_size: int = WIKI_HEALTH_HISTORY_SAMPLE_SIZE,
) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _HEALTH_HISTORY_LIST_FIELDS and isinstance(value, list):
            compact[f"{key}_count"] = len(value)
            compact[f"{key}_sample"] = value[:sample_size]
        elif _is_json_scalar(value):
            compact[key] = value
    return compact


def _is_json_scalar(value: object) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


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


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("payload")
    return payload if isinstance(payload, dict) else result


def _status(result: dict[str, Any], payload: dict[str, Any]) -> str:
    status = payload.get("status") or result.get("status")
    if isinstance(status, str) and status:
        return status
    return "completed" if result.get("ok") else "failed"


def _run_error(
    result: dict[str, Any],
    payload: dict[str, Any],
    *,
    command: str,
    status: str,
) -> str | None:
    """Error text for gwiki results that must record a failed cron run."""
    if result.get("ok") is not False and status.lower() not in _FAILED_RUN_STATUSES:
        return None
    for candidate in (result.get("error"), payload.get("error")):
        if isinstance(candidate, dict):
            message = candidate.get("message") or candidate.get("type")
            if isinstance(message, str) and message:
                return message
        if isinstance(candidate, str) and candidate:
            return candidate
    stderr = result.get("stderr")
    if isinstance(stderr, str) and stderr.strip():
        return stderr.strip()
    return f"gwiki {command} reported status '{status}'"


def _scope_project_id(scope: str, fallback_project_id: str) -> str:
    """Project scopes file tasks into their own project; topic scopes (for
    example ``topic:sessions`` for cross-project sessions) file into the
    registering project."""
    if scope.startswith("project:"):
        candidate = scope.removeprefix("project:").strip()
        if candidate:
            return candidate
    return fallback_project_id


def _scopes_from_config_value(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return _configured_scopes([value], "")
    if isinstance(value, Iterable):
        return _configured_scopes([str(item) for item in value], "")
    return []
