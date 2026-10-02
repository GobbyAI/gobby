"""Placed spawn_agent launches: preflight refusals, pane compensation and the reply."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
from dataclasses import dataclass
from typing import Any

from gobby.agents.sandbox import SandboxConfig
from gobby.agents.sandbox_gate import SandboxRequiredError, require_managed_srt
from gobby.mcp_proxy.tools.spawn_agent._spawn_phase import SpawnPhase
from gobby.storage.sessions._constants import system_session_id
from gobby.terminals.workspace_agent_panes import (
    AgentPaneReserver,
    AgentPlacement,
    AgentPlacementError,
    ResolvedPlacement,
)
from gobby.utils.git import run_to_completion

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlacedLaunch:
    """A placement that passed preflight, with the reserver that resolved it."""

    reserver: AgentPaneReserver
    resolved: ResolvedPlacement


def _refusal(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "placement_error": code, "error": message}


async def preflight_placement(
    placement: dict[str, Any] | None,
    *,
    reserver: AgentPaneReserver | None,
    sandbox_config: SandboxConfig,
    parent_session_id: str,
    machine_id: str | None,
    project_id: str | None,
    project_context_authoritative: bool,
) -> PlacedLaunch | dict[str, Any] | None:
    """Resolve a placement before any side effect; return a refusal reply or ``None``."""
    if placement is None:
        return None
    try:
        parsed = AgentPlacement.parse(placement)
    except AgentPlacementError as exc:
        return _refusal(exc.code, str(exc))
    try:
        await asyncio.to_thread(require_managed_srt, sandbox_config)
    except SandboxRequiredError as exc:
        return _refusal("sandbox_required", str(exc))
    if (
        machine_id is None
        or parent_session_id == system_session_id(machine_id)
        or not project_context_authoritative
        or project_id is None
    ):
        return _refusal(
            "parent_unresolved",
            "A placed launch needs a resolved parent session or an explicit project_path",
        )
    if reserver is None:
        return _refusal("invalid_op", "Agent pane placement is not available")
    try:
        resolved = await reserver.preflight(f"session:{parent_session_id}", project_id, parsed)
    except AgentPlacementError as exc:
        return _refusal(exc.code, str(exc))
    return PlacedLaunch(reserver=reserver, resolved=resolved)


def task_active_refusal(response: dict[str, Any]) -> dict[str, Any]:
    """Turn the active-task skipped reply into a placed refusal; pass other refusals on."""
    if response.get("skipped") is True:
        return {
            "success": False,
            "placement_error": "task_active",
            "run_id": response.get("run_id"),
        }
    return response


async def run_placed_spawn(
    placed: PlacedLaunch, phase: SpawnPhase, *, worktree_id: str | None
) -> dict[str, Any]:
    """Reserve the pane, launch inline, then settle on success or release on any other exit."""
    reserver = placed.reserver
    try:
        reserved = await reserver.reserve(placed.resolved, worktree_id=worktree_id)
    except asyncio.CancelledError:
        await phase.fail("Agent spawn cancelled")
        raise
    except AgentPlacementError as exc:
        return {**await phase.fail(str(exc)), "placement_error": exc.code}
    except Exception as exc:
        logger.warning("Pane reserve failed for run %s: %s", phase.run_id, type(exc).__name__)
        return await phase.fail(str(exc))

    # Recorded before bind awaits: a publish failure raises after the pane holds it.
    launch_terminal_id: str | None = None
    bind_error: str | None = None

    async def bind(terminal_id: str) -> None:
        nonlocal launch_terminal_id, bind_error
        launch_terminal_id = terminal_id
        try:
            await reserver.bind(reserved, terminal_id)
        except AgentPlacementError as exc:
            bind_error = exc.code
            if exc.code == "busy":
                # Another pane holds the terminal; the executor's pending failure settles it.
                launch_terminal_id = None
            raise

    phase.spawn_request.placement_binder = bind
    refs = {
        "workspace": f"{placed.resolved.node_ref}:{placed.resolved.workspace_ref}",
        "tab_ref": reserved.tab_ref,
        "pane_ref": reserved.pane_ref,
    }
    placed_phase = dataclasses.replace(
        phase, finalize=functools.partial(phase.finalize, placement=refs)
    )
    result: dict[str, Any] | None = None
    try:
        result = await placed_phase.execute_phase()
    finally:
        if result is not None and result.get("success") is True:
            reserver.settle(reserved)
        else:
            await run_to_completion(reserver.release(reserved, terminal_id=launch_terminal_id))
    if bind_error is not None and result.get("success") is not True:
        return {**result, "placement_error": bind_error}
    return result
