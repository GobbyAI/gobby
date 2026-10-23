"""Remote spawn: a run for another machine is launched through that machine's node.

Trust boundary (Security Reviewer scope):
- The origin machine is derived from the authenticated caller, never from the request body.
- Ownership of the target machine is checked before any row is written or any command is sent.
- The hub issues no managed credential and writes no prompt file, env, or directory for a remote run.
- The hub does not verify SRT for a remote run; the node verifies it and refuses a payload whose
  sandbox is not ``srt`` before anything is recorded.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from gobby.agents.external_write_grants import apply_write_grant
from gobby.agents.reasoning import resolve_spawn_reasoning
from gobby.agents.sandbox_network import definition_sandbox_config
from gobby.agents.session import ChildSessionConfig, ChildSessionManager
from gobby.config.app import DaemonConfig
from gobby.hub.machine_routing import RoutedMachine
from gobby.hub.node_client import NodeCommandError, node_command_client
from gobby.hub.spawn_payload import build_spawn_payload
from gobby.mcp_proxy.tools.spawn_agent._code_index import code_index_preflight_mode
from gobby.mcp_proxy.tools.spawn_agent._generation_endpoint import (
    resolve_spawn_generation_endpoint,
)
from gobby.mcp_proxy.tools.spawn_agent._spawn_guards import configured_max_active_agents
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.terminals import TerminalManager
from gobby.workflows.definitions import AgentDefinitionBody

_MACHINE_LOCKS: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True)
class RemoteSpawnRequest:
    """The caller's spawn inputs, before any local or remote placement."""

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
    task_category: str | None = None
    write_grant: dict[str, Any] | None = None
    resume_session_id: str | None = None


def _machine_lock(machine_id: str) -> asyncio.Lock:
    """One lock per target machine, held across the active count and the row writes."""
    return _MACHINE_LOCKS.setdefault(machine_id, asyncio.Lock())


def _release(lease: Any | None) -> None:
    if lease is not None:
        lease.release_unattached()


def _refusal(error_code: str, message: str, **extra: Any) -> dict[str, Any]:
    """A refusal that is definite: nothing was recorded and nothing was sent."""
    return {"success": False, "error_code": error_code, "error": message, "stage": "none", **extra}


def _thread_id(db: HubDatabase, session_id: str) -> str | None:
    """The provider thread id a resumed session carries: its CLI-native external id."""
    row = db.fetchone("SELECT external_id FROM sessions WHERE id = %s", (session_id,))
    return None if row is None else str(row["external_id"])


async def spawn_remote_agent(
    db: HubDatabase,
    *,
    routed: RoutedMachine,
    request: RemoteSpawnRequest,
    lease: Any | None = None,
    daemon_config: DaemonConfig,
    runner: Any,
    session_manager: ChildSessionManager,
) -> dict[str, Any]:
    """Launch one run on ``routed``'s node.

    The hub does not verify SRT here: the node verifies it and refuses a payload whose
    sandbox is not ``srt``. The hub refuses anything but an enabled ``srt`` sandbox before
    it writes a row or sends a command.
    """
    ok, reason, _depth = runner.can_spawn(request.parent_session_id)
    if not ok:
        return _refusal("spawn_refused", reason)
    sandbox = definition_sandbox_config(daemon_config, request.agent_body)
    if request.write_grant is not None:
        sandbox = apply_write_grant(sandbox, request.write_grant)
    if not sandbox.enabled or sandbox.backend != "srt":
        return _refusal("sandbox_required", "A remote run needs the enabled srt sandbox")

    reasoning = resolve_spawn_reasoning(
        provider=request.provider,
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
            runtime_provider=request.provider,
            machine_id=routed.machine_id,
        )
    except ValueError as exc:
        return _refusal("invalid_model", str(exc))
    resume_thread = _thread_id(db, request.resume_session_id) if request.resume_session_id else None
    terminals = TerminalManager(db)
    terminal_id = str(uuid.uuid4())
    if lease is not None:
        conflict: dict[str, Any] | None = lease.acquire()
        if conflict is not None:
            return conflict

    async with _machine_lock(routed.machine_id):
        active = len(terminals.list_reconcilable_by_machine(routed.machine_id))
        if active >= configured_max_active_agents():
            _release(lease)
            return _refusal("machine_cap_reached", f"{routed.machine_id} is at its agent limit")
        if request.resume_session_id:
            child_session_id = request.resume_session_id
        else:
            child = session_manager.create_child_session(
                ChildSessionConfig(
                    parent_session_id=request.parent_session_id,
                    project_id=request.project_id,
                    machine_id=routed.machine_id,
                    source=request.provider,
                    workflow_name=request.workflow_name,
                    sandbox_enabled=sandbox.enabled,
                    is_local=endpoint.is_local,
                    routed_machine_id=routed.machine_id,
                )
            )
            child_session_id = child.id
        run = runner.run_storage.create(
            parent_session_id=request.parent_session_id,
            provider=request.provider,
            prompt=request.prompt,
            workflow_name=request.workflow_name,
            agent_name=request.agent_name,
            model=endpoint.model,
            is_local=endpoint.is_local,
            requested_reasoning_effort=reasoning.requested_effort,
            effective_reasoning_effort=reasoning.effective_effort,
            reasoning_required=reasoning.reasoning_required,
            reasoning_status=reasoning.status,
            reasoning_message=reasoning.message,
            child_session_id=child_session_id,
            task_id=request.task_id,
            timeout_seconds=request.timeout_seconds,
            machine_id=routed.machine_id,
        )
        terminals.create_pending(
            terminal_id=terminal_id,
            project_id=request.project_id,
            backend="native",
            ownership="gobby",
            spawn_key=f"remote:{run.id}",
            machine_id=routed.machine_id,
            session_id=child_session_id,
            agent_run_id=run.id,
        )
        attach_error = lease.attach(run.id) if lease is not None else None
        if attach_error is not None:
            terminals.fail_pending(terminal_id)
            runner.run_storage.cancel(
                run.id, terminal_reason="spawn_rollback", result="spawn_mutex_failed"
            )
            _release(lease)
            return _refusal("spawn_mutex_failed", attach_error, run_id=run.id)

    spawn_request = {
        "prompt": request.prompt,
        "project_id": request.project_id,
        "project_path": routed.checkout_root,
        "terminal_id": terminal_id,
        "initial_variables": request.initial_variables,
        "agent_depth": session_manager.get_session_depth(child_session_id),
        "max_agent_depth": request.max_agent_depth,
        "title": request.title,
        "timeout": request.timeout_seconds,
        "droid_mode": "exec",
        "agent_lookup_name": request.agent_name,
        "agent_body": request.agent_body,
        "checkout_mode": request.isolation,
        "branch_name": request.branch_name or f"agent/{run.id}",
        "base_branch": request.base_branch,
    }
    payload = build_spawn_payload(
        db,
        run=run,
        request=spawn_request,
        endpoint=endpoint,
        reasoning=reasoning,
        sandbox_config=sandbox,
        resume_metadata=(
            {"resume_existing_session": True, "provider_native_session_id": resume_thread}
            if resume_thread
            else {}
        ),
        code_index_mode=code_index_preflight_mode(
            checkout_mode=request.isolation
            or (request.agent_body.checkout_mode if request.agent_body is not None else None)
            or "none",
            task_category=request.task_category,
        ),
        tmux_config=daemon_config.tmux,
    )
    try:
        await node_command_client().send(
            routed.machine_id, "agent.spawn", payload.model_dump(mode="json")
        )
    except NodeCommandError as exc:
        if exc.stage == "none":
            terminals.fail_pending(terminal_id)
            runner.run_storage.cancel(run.id, terminal_reason="spawn_rollback", result=exc.code)
            # The lease was attached to this run before the send, so only its row can end it.
            TaskDispatchMutexManager(db).clear_by_run_id(run.id)
            return _refusal(exc.code, str(exc), run_id=run.id)
        return {
            "success": False,
            "error_code": "spawn_in_doubt",
            "error": str(exc),
            "stage": exc.stage,
            "run_id": run.id,
            "terminal_id": terminal_id,
        }
    return {
        "success": True,
        "run_id": run.id,
        "status": "pending",
        "child_session_id": child_session_id,
        "terminal_id": terminal_id,
    }
