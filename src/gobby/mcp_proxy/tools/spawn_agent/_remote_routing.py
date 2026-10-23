"""Route one spawn to the machine that owns its run, before any local row is written.

The origin machine is the caller session's machine, never a request field. A resume of a
node session routes to that node and overrides both the requested machine and the origin.
``resolve_spawn_route`` checks ownership of the target before anything else, for local and
remote results alike, so a non-owner gets ``machine_not_owned`` with nothing recorded.
"""

from __future__ import annotations

from typing import Any

from gobby.agents.session import ChildSessionManager
from gobby.config.app import DaemonConfig
from gobby.hub.caller_scope import require_machine_owned
from gobby.hub.machine_routing import RoutedMachine, is_local_machine, resolve_target_machine
from gobby.hub.remote_spawn import RemoteSpawnRequest, spawn_remote_agent
from gobby.mcp_proxy.tools.spawn_agent._spawn_guards import TaskSpawnLease
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager


def _session_machine_id(
    session_manager: SessionManager | None, session_id: str | None
) -> str | None:
    if session_manager is None or session_id is None:
        return None
    session = session_manager.get(session_id)
    return None if session is None else session.machine_id


def is_local_target(
    session_manager: SessionManager | None,
    *,
    machine: str | None,
    resume_session_id: str | None,
) -> bool:
    """Whether the run stays on this machine, so no hub database is needed to place it.

    Mirrors ``resolve_spawn_route``: a resume's machine overrides the requested machine.
    """
    resume_machine = _session_machine_id(session_manager, resume_session_id)
    return is_local_machine(resume_machine or machine)


def spawn_refusal(code: str, message: str, stage: str | None = "none") -> dict[str, Any]:
    return {"success": False, "error_code": code, "error": message, "stage": stage}


async def resolve_spawn_route(
    db: HubDatabase,
    *,
    session_manager: SessionManager | None,
    project_id: str,
    machine: str | None,
    caller_session_id: str | None,
    resume_session_id: str | None,
) -> RoutedMachine:
    """Resolve the target machine and check that the caller owns it. Writes nothing.

    Raises ``NodeCommandError`` for an unknown, unreachable, or foreign target. An empty
    ``project_id`` has no checkout, so a remote target refuses as ``checkout_unresolved``.
    """
    resume_machine = _session_machine_id(session_manager, resume_session_id)
    origin = resume_machine or _session_machine_id(session_manager, caller_session_id)
    routed = await resolve_target_machine(
        db,
        project_id=project_id,
        requested=resume_machine or machine,
        origin_machine_id=origin,
    )
    require_machine_owned(routed.machine_id)
    return routed


async def spawn_remote_route(
    db: HubDatabase,
    *,
    routed: RoutedMachine,
    session_manager: SessionManager | None,
    runner: Any,
    daemon_config: DaemonConfig | None,
    placement: dict[str, Any] | None,
    request: RemoteSpawnRequest,
) -> dict[str, Any]:
    """Launch the run on ``routed``'s node. Call only when ``routed`` is not local."""
    if placement is not None:
        return spawn_refusal(
            "remote_placement_unsupported",
            "Placement is not supported for a run on another machine",
        )
    if daemon_config is None or session_manager is None:
        return spawn_refusal("config_unavailable", "The daemon config snapshot is unavailable")
    lease = TaskSpawnLease(db=db, task_id=request.task_id)
    return await spawn_remote_agent(
        db,
        routed=routed,
        request=request,
        lease=lease,
        daemon_config=daemon_config,
        runner=runner,
        session_manager=ChildSessionManager(
            session_storage=session_manager,
            max_agent_depth=request.max_agent_depth,
        ),
    )
