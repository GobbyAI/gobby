from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from gobby.gwiki_gateway import GwikiGateway
from gobby.scheduler.executor import CronHandler
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.wiki.update_coordinator import WikiUpdateCoordinator

WIKI_RESEARCH_INTERVAL_SECONDS = 6 * 60 * 60
WIKI_REFRESH_INTERVAL_SECONDS = 60 * 60
WIKI_HEALTH_INTERVAL_SECONDS = 30 * 60
WIKI_AUDIT_INTERVAL_SECONDS = 24 * 60 * 60


class WikiGatewayProtocol(Protocol):
    async def research(self, query: str | None = None) -> dict[str, Any]: ...

    async def refresh(
        self,
        *,
        scope: str | None = None,
        source_ids: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]: ...

    async def health(self) -> dict[str, Any]: ...

    async def audit(self) -> dict[str, Any]: ...

    async def index(self) -> dict[str, Any]: ...


class CronRegistrationProtocol(Protocol):
    def register_handler(self, name: str, handler: CronHandler) -> None: ...


GatewayFactory = Callable[[str], WikiGatewayProtocol]


def create_wiki_research_handler(
    *,
    gateway: WikiGatewayProtocol,
    scope: str,
) -> CronHandler:
    async def research_handler(job: CronJob) -> str:
        query = _string_or_none(job.action_config.get("query"))
        result = await gateway.research(query)
        return _history_output(
            purpose="Run scheduled wiki research",
            scope=scope,
            command="research",
            gwiki_result=result,
        )

    return research_handler


def create_wiki_refresh_handler(
    *,
    gateway: WikiGatewayProtocol,
    coordinator: WikiUpdateCoordinator,
    scope: str,
) -> CronHandler:
    async def refresh_handler(job: CronJob) -> str:
        result = await gateway.refresh(scope=scope, source_ids=None)
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
        return _history_output(
            purpose="Run wiki health checks",
            scope=scope,
            command="health",
            gwiki_result=result,
        )

    return health_handler


def create_wiki_audit_handler(
    *,
    gateway: WikiGatewayProtocol,
    scope: str,
) -> CronHandler:
    async def audit_handler(job: CronJob) -> str:
        result = await gateway.audit()
        return _history_output(
            purpose="Audit wiki content",
            scope=scope,
            command="audit",
            gwiki_result=result,
        )

    return audit_handler


def register_wiki_cron_jobs(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistrationProtocol,
    project_id: str,
    scopes: Iterable[str] | None = None,
    gateway_factory: GatewayFactory | None = None,
) -> int:
    """Register wiki cron handlers and reconcile one cron row per scope and command."""
    registered = 0
    for scope in _configured_scopes(scopes, project_id):
        gateway = _create_gateway(scope, gateway_factory)
        coordinator = WikiUpdateCoordinator(gateway)

        for command, purpose, interval, handler in (
            (
                "research",
                "Scheduled wiki research",
                WIKI_RESEARCH_INTERVAL_SECONDS,
                create_wiki_research_handler(gateway=gateway, scope=scope),
            ),
            (
                "refresh",
                "Scheduled wiki source refresh",
                WIKI_REFRESH_INTERVAL_SECONDS,
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
                create_wiki_health_handler(gateway=gateway, scope=scope),
            ),
            (
                "audit",
                "Scheduled wiki audit",
                WIKI_AUDIT_INTERVAL_SECONDS,
                create_wiki_audit_handler(gateway=gateway, scope=scope),
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
            )
            registered += 1

    return registered


def configured_wiki_cron_scopes(config: object | None, project_id: str) -> list[str]:
    if config is None:
        return [project_id]

    wiki_config = getattr(config, "wiki", None)
    scopes = _scopes_from_config_value(getattr(wiki_config, "scheduled_scopes", None))
    if scopes:
        return scopes

    scopes = _scopes_from_config_value(getattr(config, "wiki_scheduled_scopes", None))
    return scopes or [project_id]


def wiki_handler_name(command: str, scope: str) -> str:
    return f"wiki:{command}:{scope}"


def wiki_job_name(command: str, scope: str) -> str:
    return f"gobby:wiki-{command}:{scope}"


def _create_gateway(scope: str, gateway_factory: GatewayFactory | None) -> WikiGatewayProtocol:
    if gateway_factory is not None:
        return gateway_factory(scope)
    return GwikiGateway(project=scope)


def _ensure_wiki_cron_job(
    *,
    cron_storage: CronJobStorage,
    project_id: str,
    command: str,
    scope: str,
    handler_name: str,
    purpose: str,
    interval_seconds: int,
) -> None:
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
            schedule_type="interval",
            interval_seconds=interval_seconds,
            action_type="handler",
            action_config=action_config,
            enabled=True,
            is_system=True,
        )
        return

    if not existing.is_system:
        cron_storage.update_job(
            existing.id,
            description=description,
            schedule_type="interval",
            cron_expr=None,
            interval_seconds=interval_seconds,
            run_at=None,
            action_type="handler",
            action_config=action_config,
            enabled=True,
        )
        return

    if existing.is_system:
        cron_storage.reconcile_system_job_definition(
            existing.id,
            action_type="handler",
            action_config=action_config,
            description=description,
            schedule_type="interval",
            interval_seconds=interval_seconds,
        )


def _configured_scopes(scopes: Iterable[str] | None, project_id: str) -> list[str]:
    values = list(scopes) if scopes is not None else [project_id]
    normalized = [scope.strip() for scope in values if scope and scope.strip()]
    return list(dict.fromkeys(normalized)) or [project_id]


def _history_output(
    *,
    purpose: str,
    scope: str,
    command: str,
    gwiki_result: dict[str, Any],
    changed_paths: list[str] | None = None,
) -> str:
    payload = _payload(gwiki_result)
    output: dict[str, Any] = {
        "purpose": purpose,
        "scope": scope,
        "command": command,
        "status": _status(gwiki_result, payload),
        "result": _visible_result(gwiki_result, payload),
    }
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
    if "index_handoff" in result:
        visible["index_handoff"] = result["index_handoff"]
    return visible


def _refresh_changed_paths(result: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for entry in _payload(result).get("refreshed", []):
        if not isinstance(entry, dict) or not entry.get("changed"):
            continue
        raw_path = entry.get("raw_path")
        if isinstance(raw_path, str) and raw_path:
            paths.append(raw_path)
    return paths


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("payload")
    return payload if isinstance(payload, dict) else result


def _status(result: dict[str, Any], payload: dict[str, Any]) -> str:
    status = payload.get("status") or result.get("status")
    if isinstance(status, str) and status:
        return status
    return "completed" if result.get("ok") else "failed"


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _scopes_from_config_value(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return _configured_scopes([value], "")
    if isinstance(value, Iterable):
        return _configured_scopes([str(item) for item in value], "")
    return []
