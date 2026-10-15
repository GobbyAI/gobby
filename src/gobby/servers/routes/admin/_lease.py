"""Single-active-daemon lease routes for the full runtime."""

from __future__ import annotations

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
    """Register cooperative handoff; gdaemon owns status, promote, and recover."""

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
        from gobby.runner_maintenance import write_shutdown_source

        # gdaemon owns the lease: a stop marker makes it release and exit, while an
        # unmarked backend exit reads as a crash and is respawned under the lease.
        write_shutdown_source("lease_handoff", intent="stop")
        background_tasks.add_task(runner.request_shutdown)
        return {"handoff": "accepted"}


__all__ = ["register_lease_routes"]
