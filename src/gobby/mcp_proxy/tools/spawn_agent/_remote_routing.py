"""Route one spawn to the machine that owns its run, and prepare a remote run on the hub.

The origin machine is the caller's machine: the caller session's machine on MCP, or the
verified front-door machine on HTTP. It is never a request field. A resume names a session,
and its machine is the target. The reference is canonicalized first, so a ``#N`` or prefix
routes to its owner. ``resolve_spawn_route`` checks ownership of the target before anything
else. ``spawn_remote_route`` then validates the resume, resolves the task, and reserves the
project slot before any row is written.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from gobby.agents.session import ChildSessionManager
from gobby.config.app import DaemonConfig
from gobby.hub.caller_scope import require_machine_owned
from gobby.hub.machine_routing import RoutedMachine, is_local_machine, resolve_target_machine
from gobby.hub.remote_spawn import RemoteSpawnRequest, spawn_remote_agent
from gobby.mcp_proxy.tools.spawn_agent._launch_settings import (
    LaunchRefused,
    resolve_launch_model,
    resume_launch_defaults,
    standing_seat_suffix,
)
from gobby.mcp_proxy.tools.spawn_agent._provider_resolution import (
    concrete_provider,
    missing_provider_for_supplied_model,
)
from gobby.mcp_proxy.tools.spawn_agent._resume import resolve_resume_target
from gobby.mcp_proxy.tools.spawn_agent._run_lifetime import resolve_run_lifetime
from gobby.mcp_proxy.tools.spawn_agent._spawn_guards import (
    TaskSpawnLease,
    reserve_agent_slot,
    resolve_spawn_task_context,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager


def session_machine_id(
    session_manager: SessionManager | None, session_id: str | None
) -> str | None:
    if session_manager is None or session_id is None:
        return None
    session = session_manager.get(session_id)
    return None if session is None else session.machine_id


def _resume_machine_id(
    session_manager: SessionManager | None, ref: str | None, project_id: str | None
) -> str | None:
    """The machine of the session a resume reference names, canonicalized in the project."""
    if session_manager is None or ref is None:
        return None
    try:
        session_id = session_manager.resolve_session_reference(ref, project_id)
    except ValueError:
        return None
    return session_machine_id(session_manager, session_id)


def is_local_target(
    session_manager: SessionManager | None,
    *,
    machine: str | None,
    resume_session_id: str | None,
    project_id: str | None,
) -> bool:
    """Whether the run stays on this machine, so no hub database is needed to place it."""
    resume_machine = _resume_machine_id(session_manager, resume_session_id, project_id)
    return is_local_machine(resume_machine or machine)


def spawn_refusal(code: str, message: str, stage: str | None = "none") -> dict[str, Any]:
    return {"success": False, "error_code": code, "error": message, "stage": stage}


async def resolve_spawn_route(
    db: HubDatabase,
    *,
    session_manager: SessionManager | None,
    project_id: str,
    machine: str | None,
    origin_machine_id: str | None,
    resume_session_id: str | None,
) -> RoutedMachine:
    """Resolve the target machine and check that the origin owns it. Writes nothing.

    Raises ``NodeCommandError`` for an unknown, unreachable, or foreign target. An empty
    ``project_id`` has no checkout, so a remote target refuses as ``checkout_unresolved``.
    """
    resume_machine = _resume_machine_id(session_manager, resume_session_id, project_id)
    routed = await resolve_target_machine(
        db,
        project_id=project_id,
        requested=resume_machine or machine,
        origin_machine_id=origin_machine_id,
    )
    require_machine_owned(routed.machine_id)
    return routed


async def spawn_remote_route(
    db: HubDatabase,
    *,
    routed: RoutedMachine,
    session_manager: SessionManager | None,
    task_manager: Any | None,
    runner: Any,
    daemon_config: DaemonConfig | None,
    placement: dict[str, Any] | None,
    request: RemoteSpawnRequest,
    completion_registry: Any | None = None,
) -> dict[str, Any]:
    """Launch the run on ``routed``'s node. Call only when ``routed`` is not local.

    Every check that can refuse runs before a row is written: the resume target, the launch
    settings, the task, and the project slot. Only then does ``spawn_remote_agent`` prepare
    the run with the shared path and send it.
    """
    if placement is not None:
        return spawn_refusal(
            "remote_placement_unsupported",
            "Placement is not supported for a run on another machine",
        )
    if daemon_config is None or session_manager is None:
        return spawn_refusal("config_unavailable", "The daemon config snapshot is unavailable")
    if request.extra_write_paths:
        # A managed grant is recorded on one machine's disk; a node run cannot inherit it.
        return spawn_refusal(
            "remote_write_grant_unsupported",
            "External write paths are not supported for a run on another machine",
        )

    resume_target = None
    if request.resume_session_id is not None:
        agent_type = request.agent_body.name if request.agent_body is not None else "default"
        try:
            resume_target = resolve_resume_target(
                session_manager,
                request.resume_session_id,
                request.project_id,
                concrete_provider(request.requested_provider),
                runner.run_storage,
                agent_type,
                request.lane,
            )
        except ValueError as exc:
            return spawn_refusal("resume_refused", str(exc))

    provider, model, reasoning_effort = request.provider, request.model, request.reasoning_effort
    if resume_target is not None:
        provider, model, reasoning_effort = resume_launch_defaults(
            resume_target, model=model, reasoning_effort=reasoning_effort
        )
    missing_provider = missing_provider_for_supplied_model(
        explicit_provider=concrete_provider(provider), model=model
    )
    if missing_provider is not None:
        return missing_provider.to_response()
    try:
        launch = await resolve_launch_model(
            provider=provider,
            model=model,
            agent_body=request.agent_body,
            reasoning_effort=reasoning_effort,
            reasoning_required=request.reasoning_required,
        )
        run_lifetime = resolve_run_lifetime(
            request.agent_body, request.execution_mode, resumed=resume_target is not None
        )
    except LaunchRefused as exc:
        return {"stage": "none", "error_code": "launch_refused", **exc.response}
    except ValueError as exc:
        return spawn_refusal("launch_refused", str(exc))

    task = await resolve_spawn_task_context(
        prompt=request.prompt,
        task_id=request.task_id,
        task_manager=task_manager,
        project_id=request.project_id,
        allow_closed_task=request.allow_closed_task,
        agent_body=request.agent_body,
        initial_variables=request.initial_variables,
    )
    if task.refusal is not None:
        refusal = dict(task.refusal)
        refusal.setdefault("error_code", "task_refused")
        refusal.setdefault("stage", "none")
        return refusal
    variables = dict(request.initial_variables)
    variables["execution_mode"] = run_lifetime.execution_mode
    if task.resolved_task_id is not None:
        variables["assigned_task_id"] = (
            f"#{task.task_seq_num}" if task.task_seq_num else task.resolved_task_id
        )
        variables["assigned_task_uuid"] = task.resolved_task_id
        variables["parent_session_id"] = request.parent_session_id
    if task.task_additional_skills is not None:
        variables["additional_skills"] = task.task_additional_skills

    request = replace(
        request,
        prompt=task.prompt + standing_seat_suffix(run_lifetime),
        task_id=task.resolved_task_id,
        claimed_session_id=task.claimed_session_id,
        task_category=task.task_category,
        initial_variables=variables,
        resume_session_id=resume_target.id if resume_target is not None else None,
        provider=provider,
        model=launch.model,
        reasoning_effort=launch.reasoning_effort,
        reasoning_required=launch.reasoning_required,
        execution_mode=run_lifetime.execution_mode,
        idle_ttl_seconds=run_lifetime.idle_ttl_seconds,
    )
    lease = TaskSpawnLease(db=db, task_id=request.task_id)
    async with reserve_agent_slot(
        db=db, project_id=request.project_id, project_path=routed.checkout_root or ""
    ) as slot:
        if slot is not None:
            return {**slot, "error_code": "project_cap_reached", "stage": "none"}
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
            resume_target=resume_target,
            task_manager=task_manager,
            completion_registry=completion_registry,
        )
