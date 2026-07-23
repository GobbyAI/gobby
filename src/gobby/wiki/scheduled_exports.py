"""Scheduled refresh handler for agent-facing wiki exports."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from gobby.storage.cron_models import CronJob
from gobby.wiki.scheduled_jobs_history import _payload, _run_error, _status

WikiExportHandler = Callable[[CronJob], Awaitable[str]]


class AgentExportGatewayProtocol(Protocol):
    async def export_pages(self) -> dict[str, Any]: ...

    async def graph_artifacts(self) -> dict[str, Any]: ...


async def _run_export_step(
    command: str,
    operation: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    try:
        result = await operation()
    except Exception as exc:
        return {"ok": False, "status": "failed", "error": str(exc)}

    payload = _payload(result)
    status = _status(result, payload)
    error = _run_error(result, payload, command=command, status=status)
    step: dict[str, Any] = {"ok": error is None, "status": status}
    if error is not None:
        step["error"] = error
    return step


def create_wiki_exports_handler(
    *,
    gateway: AgentExportGatewayProtocol,
    scope: str,
) -> WikiExportHandler:
    async def exports_handler(_job: CronJob) -> str:
        steps = {
            "export_pages": await _run_export_step("export_pages", gateway.export_pages),
            "graph_artifacts": await _run_export_step(
                "graph_artifacts",
                gateway.graph_artifacts,
            ),
        }
        failures = {name: step for name, step in steps.items() if not step["ok"]}
        if len(failures) == len(steps):
            details = "; ".join(f"{name}: {step['error']}" for name, step in failures.items())
            raise RuntimeError(f"agent export refresh failed: {details}")

        return json.dumps(
            {
                "purpose": "Refresh agent-facing wiki exports",
                "scope": scope,
                "command": "exports",
                "status": "degraded" if failures else "completed",
                "ok": True,
                "result": {"steps": steps},
            },
            sort_keys=True,
        )

    return exports_handler
