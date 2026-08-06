"""Single-active-daemon lease routes for the full runtime."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from fastapi import APIRouter, BackgroundTasks, HTTPException

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner
    from gobby.servers.http import HTTPServer


def _active_work_blockers(runner: GobbyRunner) -> dict[str, int]:
    """Return local work that makes cooperative handoff unsafe."""
    blockers: dict[str, int] = {}
    agent_runner = runner.agent_runner
    active_agents = agent_runner.get_running_agents_count() if agent_runner is not None else 0
    if active_agents:
        blockers["active_agent_runs"] = active_agents

    cron_scheduler = runner.cron_scheduler
    if cron_scheduler is not None:
        if runner.machine_id is None:
            raise RuntimeError("local machine identity is unavailable")
        active_crons = cron_scheduler.storage.count_running(runner.machine_id)
        if active_crons:
            blockers["active_cron_runs"] = active_crons
    return blockers


def register_lease_routes(router: APIRouter, server: HTTPServer) -> None:
    """Register active-runtime lease status and cooperative handoff."""

    @router.get("/lease/status")
    async def lease_status() -> dict[str, object]:
        runner = server.get_runner()
        if runner is None:
            raise HTTPException(status_code=503, detail="runner unavailable")
        status = await server.run_db(runner.daemon_lease.status)
        return {"mode": "active", **asdict(status)}

    @router.post("/lease/promote")
    async def promote() -> dict[str, object]:
        return {"promoting": False, "mode": "active"}

    @router.post("/lease/handoff")
    async def handoff(background_tasks: BackgroundTasks) -> dict[str, str]:
        runner = server.get_runner()
        if runner is None:
            raise HTTPException(status_code=503, detail="runner unavailable")
        blockers = await server.run_db(_active_work_blockers, runner)
        if blockers:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "cooperative handoff requires local quiescence",
                    "blockers": blockers,
                },
            )
        background_tasks.add_task(runner.request_shutdown)
        return {"handoff": "accepted"}

    @router.post("/lease/recover")
    async def recover() -> None:
        raise HTTPException(status_code=409, detail="lease owner is already active")


__all__ = ["register_lease_routes"]
