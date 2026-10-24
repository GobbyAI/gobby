"""Remote spawn: a run for another machine is prepared on the hub and launched by its node.

Trust boundary (Security Reviewer scope):
- The origin machine is derived from the authenticated caller, never from the request body.
- Ownership of the target machine is checked before any row is written or any command is sent.
- The hub issues no managed credential and writes no prompt file, env, or directory for a remote run.
- The hub does not verify SRT for a remote run; the node verifies it and refuses a payload whose
  sandbox is not ``srt`` before anything is recorded.

Preparation is the local path's: ``prepare_terminal_spawn`` or ``prepare_terminal_resume``
creates the child session, the run, the variables and the session binding. The divergence is
the launch: the node receives the payload instead of a local terminal.
"""

from __future__ import annotations

import asyncio
import functools
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from gobby.agents.reasoning import resolve_spawn_reasoning
from gobby.agents.sandbox_network import definition_sandbox_config
from gobby.agents.session import ChildSessionManager
from gobby.agents.spawn import PreparedSpawn, prepare_terminal_resume, prepare_terminal_spawn
from gobby.config.app import DaemonConfig
from gobby.hub.machine_routing import RoutedMachine
from gobby.hub.node_client import NodeCommandError, node_command_client
from gobby.hub.spawn_payload import build_spawn_payload
from gobby.mcp_proxy.tools.spawn_agent._code_index import code_index_preflight_mode
from gobby.mcp_proxy.tools.spawn_agent._failure_cleanup import _roll_back_run
from gobby.mcp_proxy.tools.spawn_agent._generation_endpoint import (
    resolve_spawn_generation_endpoint,
)
from gobby.mcp_proxy.tools.spawn_agent._resume import resumed_seat_lane
from gobby.mcp_proxy.tools.spawn_agent._spawn_guards import (
    configured_max_active_agents,
)
from gobby.storage.agents import TERMINAL_AGENT_RUN_STATUSES
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.terminals import TerminalManager
from gobby.utils.git import run_to_completion
from gobby.workflows.definitions import AgentDefinitionBody

_MACHINE_LOCKS: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True)
class RemoteSpawnRequest:
    """The caller's spawn inputs, after task and resume resolution."""

    parent_session_id: str
    project_id: str
    prompt: str
    provider: str
    agent_name: str | None = None
    agent_body: AgentDefinitionBody | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    reasoning_required: bool | None = None
    title: str | None = None
    isolation: str | None = None
    branch_name: str | None = None
    base_branch: str | None = None
    timeout_seconds: float | None = None
    max_agent_depth: int = 5
    initial_variables: dict[str, Any] = field(default_factory=dict)
    workflow_name: str | None = None
    task_id: str | None = None
    claimed_session_id: str | None = None
    task_category: str | None = None
    resume_session_id: str | None = None
    caller_session_id: str | None = None
    requested_provider: str | None = None
    allow_closed_task: bool = False
    extra_write_paths: list[str] | None = None
    write_paths_reason: str | None = None
    reserved_run_id: str | None = None
    lane: str | None = None
    execution_mode: Literal["one_shot", "interactive"] | None = None
    idle_ttl_seconds: int | None = None


def _machine_lock(machine_id: str) -> asyncio.Lock:
    """One lock per target machine, held across the active count and the row writes."""
    return _MACHINE_LOCKS.setdefault(machine_id, asyncio.Lock())


def _release(lease: Any | None) -> None:
    if lease is not None:
        lease.release_unattached()


def _refusal(error_code: str, message: str, **extra: Any) -> dict[str, Any]:
    """A refusal that is definite: nothing was recorded and nothing was sent."""
    return {"success": False, "error_code": error_code, "error": message, "stage": "none", **extra}


def _resume_metadata(
    request: RemoteSpawnRequest, resume_target: Session | None, run_storage: Any
) -> dict[str, Any]:
    """The run's durable launch metadata, as the local path records it."""
    metadata: dict[str, Any] = {}
    if request.execution_mode is not None:
        metadata["execution_mode"] = request.execution_mode
    if request.idle_ttl_seconds is not None:
        metadata["idle_ttl_seconds"] = request.idle_ttl_seconds
    if request.allow_closed_task:
        metadata["allow_closed_task"] = True
    lane = (request.lane or "").strip() or resumed_seat_lane(resume_target, run_storage)
    if lane:
        metadata["lane"] = lane
    if resume_target is not None:
        metadata.update(
            {
                "provider_native_session_id": resume_target.external_id,
                "resume_existing_session": True,
                "resume_previous_run_id": resume_target.agent_run_id,
                "resume_previous_parent_session_id": resume_target.parent_session_id,
                "resume_previous_status": resume_target.status,
                "resume_previous_workflow_name": resume_target.workflow_name,
            }
        )
    return metadata


async def _settle_unlaunched(
    db: HubDatabase,
    *,
    runner: Any,
    lease: Any | None,
    terminal_id: str,
    run_id: str,
    child_session_id: str,
    error: str,
    code: str,
    completion_registry: Any | None,
    task_manager: Any | None,
) -> dict[str, Any]:
    """End a run that was never sent: settle its rows, its runtime, and its task lease.

    Settlement runs to completion through caller cancellation, so the lease is never released
    while the rows it guards are still live.
    """

    async def settle() -> dict[str, Any]:
        TerminalManager(db).fail_pending(terminal_id)
        await _roll_back_run(
            runner,
            run_id,
            error,
            completion_registry=completion_registry,
            task_manager=task_manager,
            child_session_id=child_session_id,
            terminal_id=terminal_id,
        )
        run = runner.run_storage.get(run_id)
        if run is not None and run.status not in TERMINAL_AGENT_RUN_STATUSES:
            # Rollback logs and swallows its own failures; the row must still end.
            runner.run_storage.cancel(run_id, terminal_reason="spawn_rollback", result=code)
        # The lease was attached to this run before the send, so only its row can end it.
        TaskDispatchMutexManager(db).clear_by_run_id(run_id)
        _release(lease)
        return _refusal(code, error, run_id=run_id)

    return await run_to_completion(settle())


async def _drain_cancelled_prepare(
    db: HubDatabase,
    *,
    prep_task: asyncio.Future[PreparedSpawn],
    runner: Any,
    lease: Any | None,
    terminal_id: str,
    run_id: str,
    completion_registry: Any | None,
    task_manager: Any | None,
) -> None:
    """Let an in-flight preparation finish, then settle the rows it left behind."""
    await asyncio.wait({prep_task})
    if prep_task.cancelled() or prep_task.exception() is not None:
        return
    await _settle_unlaunched(
        db,
        runner=runner,
        lease=lease,
        terminal_id=terminal_id,
        run_id=run_id,
        child_session_id=prep_task.result().session_id,
        error="spawn cancelled",
        code="spawn_cancelled",
        completion_registry=completion_registry,
        task_manager=task_manager,
    )


async def spawn_remote_agent(
    db: HubDatabase,
    *,
    routed: RoutedMachine,
    request: RemoteSpawnRequest,
    lease: Any | None = None,
    daemon_config: DaemonConfig,
    runner: Any,
    session_manager: ChildSessionManager,
    resume_target: Session | None = None,
    task_manager: Any | None = None,
    completion_registry: Any | None = None,
) -> dict[str, Any]:
    """Launch one run on ``routed``'s node.

    The caller has resolved the task and the resume target. This prepares
    the run with the shared spawn path, then sends it. The hub refuses anything but an enabled
    ``srt`` sandbox before it writes a row or sends a command.
    """
    ok, reason, _depth = runner.can_spawn(request.parent_session_id)
    if not ok:
        return _refusal("spawn_refused", reason)
    sandbox = definition_sandbox_config(daemon_config, request.agent_body)
    if not sandbox.enabled or sandbox.backend != "srt":
        return _refusal("sandbox_required", "A remote run needs the enabled srt sandbox")

    provider = resume_target.source if resume_target is not None else request.provider
    reasoning = resolve_spawn_reasoning(
        provider=provider,
        model=request.model,
        requested_effort=request.reasoning_effort,
        reasoning_required=request.reasoning_required,
    )
    try:
        endpoint = await resolve_spawn_generation_endpoint(
            model=request.model,
            api_base=None,
            api_token=None,
            daemon_config=daemon_config,
            run_manager=runner.run_storage,
            runtime_provider=provider,
            machine_id=routed.machine_id,
        )
    except ValueError as exc:
        return _refusal("invalid_model", str(exc))

    run_id = request.reserved_run_id or str(uuid.uuid4())
    branch = request.branch_name or f"agent/{run_id}"
    metadata = _resume_metadata(request, resume_target, runner.run_storage)
    terminals = TerminalManager(db)
    terminal_id = str(uuid.uuid4())
    if lease is not None:
        conflict: dict[str, Any] | None = lease.acquire()
        if conflict is not None:
            return conflict

    try:
        async with _machine_lock(routed.machine_id):
            active = len(terminals.list_reconcilable_by_machine(routed.machine_id))
            if active >= configured_max_active_agents():
                _release(lease)
                return _refusal("machine_cap_reached", f"{routed.machine_id} is at its agent limit")
            shared: dict[str, Any] = {
                "parent_session_id": request.parent_session_id,
                "project_id": request.project_id,
                "source": provider,
                "workflow_name": request.workflow_name,
                "agent_name": request.agent_name,
                "initial_variables": request.initial_variables,
                "git_branch": branch,
                "prompt": request.prompt,
                "model": endpoint.model,
                "is_local": endpoint.is_local,
                "max_agent_depth": request.max_agent_depth,
                "agent_run_id": run_id,
                "task_id": request.task_id,
                "claimed_session_id": request.claimed_session_id,
                "timeout_seconds": request.timeout_seconds,
                "sandbox_enabled": sandbox.enabled,
                "requested_reasoning_effort": reasoning.requested_effort,
                "effective_reasoning_effort": reasoning.effective_effort,
                "reasoning_required": reasoning.reasoning_required,
                "reasoning_status": reasoning.status,
                "reasoning_message": reasoning.message,
                "resume_metadata_json": metadata,
                "worktree_id": None,
                "clone_id": None,
                "workspace_path": routed.checkout_root,
                "routed_machine_id": routed.machine_id,
            }
            if resume_target is None:
                prepare = functools.partial(
                    prepare_terminal_spawn, session_manager, machine_id=routed.machine_id, **shared
                )
            else:
                prepare = functools.partial(
                    prepare_terminal_resume,
                    session_manager,
                    existing_session_id=resume_target.id,
                    original_run_id=resume_target.agent_run_id,
                    relaunch=True,
                    paused_seat_not_live=resume_target.status == "paused",
                    **shared,
                )
            prep_task = asyncio.ensure_future(asyncio.to_thread(prepare))
            try:
                prepared: PreparedSpawn = await asyncio.shield(prep_task)
            except asyncio.CancelledError:
                # The thread cannot be stopped. Drain it and settle its rows through any repeat
                # cancellation, then propagate.
                await run_to_completion(
                    _drain_cancelled_prepare(
                        db,
                        prep_task=prep_task,
                        runner=runner,
                        lease=lease,
                        terminal_id=terminal_id,
                        run_id=run_id,
                        completion_registry=completion_registry,
                        task_manager=task_manager,
                    )
                )
                raise
            except Exception as exc:
                _release(lease)
                return _refusal("prepare_failed", str(exc))

            child_session_id = prepared.session_id
            try:
                terminals.create_pending(
                    terminal_id=terminal_id,
                    project_id=request.project_id,
                    backend="native",
                    ownership="gobby",
                    spawn_key=f"remote:{run_id}",
                    machine_id=routed.machine_id,
                    session_id=child_session_id,
                    agent_run_id=run_id,
                )
            except Exception as exc:
                return await _settle_unlaunched(
                    db,
                    runner=runner,
                    lease=lease,
                    terminal_id=terminal_id,
                    run_id=run_id,
                    child_session_id=child_session_id,
                    error=str(exc),
                    code="spawn_failed",
                    completion_registry=completion_registry,
                    task_manager=task_manager,
                )
            attach_error = lease.attach(run_id) if lease is not None else None
            if attach_error is not None:
                return await _settle_unlaunched(
                    db,
                    runner=runner,
                    lease=lease,
                    terminal_id=terminal_id,
                    run_id=run_id,
                    child_session_id=child_session_id,
                    error=attach_error,
                    code="spawn_mutex_failed",
                    completion_registry=completion_registry,
                    task_manager=task_manager,
                )
            try:
                run = runner.run_storage.get(run_id)
                if run is None:
                    raise RuntimeError(f"agent run {run_id} was not recorded")
                payload = build_spawn_payload(
                    db,
                    run=run,
                    request={
                        "prompt": request.prompt,
                        "project_id": request.project_id,
                        "project_path": routed.checkout_root,
                        "terminal_id": terminal_id,
                        "initial_variables": request.initial_variables,
                        "agent_depth": prepared.agent_depth,
                        "max_agent_depth": request.max_agent_depth,
                        "title": request.title,
                        "timeout": request.timeout_seconds,
                        "droid_mode": "exec",
                        "agent_lookup_name": request.agent_name,
                        "agent_body": request.agent_body,
                        "checkout_mode": request.isolation,
                        "branch_name": branch,
                        "base_branch": request.base_branch,
                    },
                    endpoint=endpoint,
                    reasoning=reasoning,
                    sandbox_config=sandbox,
                    resume_metadata=metadata,
                    code_index_mode=code_index_preflight_mode(
                        checkout_mode=request.isolation
                        or (
                            request.agent_body.checkout_mode
                            if request.agent_body is not None
                            else None
                        )
                        or "none",
                        task_category=request.task_category,
                    ),
                    tmux_config=daemon_config.tmux,
                )
                client = node_command_client()
            except Exception as exc:
                return await _settle_unlaunched(
                    db,
                    runner=runner,
                    lease=lease,
                    terminal_id=terminal_id,
                    run_id=run_id,
                    child_session_id=child_session_id,
                    error=str(exc),
                    code="spawn_failed",
                    completion_registry=completion_registry,
                    task_manager=task_manager,
                )
    except BaseException:
        # Cancelled or failed before any run owned the lease: nothing was sent, so release it.
        _release(lease)
        raise

    try:
        await client.send(routed.machine_id, "agent.spawn", payload.model_dump(mode="json"))
    except NodeCommandError as exc:
        if exc.stage == "none":
            return await _settle_unlaunched(
                db,
                runner=runner,
                lease=lease,
                terminal_id=terminal_id,
                run_id=run_id,
                child_session_id=child_session_id,
                error=str(exc),
                code=exc.code,
                completion_registry=completion_registry,
                task_manager=task_manager,
            )
        return {
            "success": False,
            "error_code": "spawn_in_doubt",
            "error": str(exc),
            "stage": exc.stage,
            "run_id": run_id,
            "terminal_id": terminal_id,
        }
    except Exception as exc:
        # The command may have reached the node, so the rows and the lease stay.
        return {
            "success": False,
            "error_code": "spawn_in_doubt",
            "error": str(exc),
            "stage": None,
            "run_id": run_id,
            "terminal_id": terminal_id,
        }
    return {
        "success": True,
        "run_id": run_id,
        "status": "pending",
        "child_session_id": child_session_id,
        "terminal_id": terminal_id,
    }
