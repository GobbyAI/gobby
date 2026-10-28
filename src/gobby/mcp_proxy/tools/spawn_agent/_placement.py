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
    PLACEMENT_WAIT_SECONDS,
    AgentPaneReserver,
    AgentPlacement,
    AgentPlacementError,
    ResolvedPlacement,
)
from gobby.utils.git import run_to_completion
from gobby.workflows.condition_helpers_sessions import own_seat_resume, own_team_tab

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlacedLaunch:
    """A placement that passed preflight, with the reserver that resolved it."""

    reserver: AgentPaneReserver
    resolved: ResolvedPlacement
    # An empty tab the resumed seat replaces; closed before the reserve takes the title.
    replaces_tab: str | None = None
    # Bounds the reserve's workspace-lock wait and its row-lock waits.
    wait_seconds: float = PLACEMENT_WAIT_SECONDS


def placement_wait_seconds(spawn_timeout: float | None) -> float:
    """The placement wait bound: the spawn's own timeout, capped at the placement default."""
    if spawn_timeout is None:
        return PLACEMENT_WAIT_SECONDS
    return min(spawn_timeout, PLACEMENT_WAIT_SECONDS)


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
    session_manager: Any | None = None,
    agent_name: str | None = None,
    seat_title: str | None = None,
    resume_session_id: str | None = None,
    replaces_tab: str | None = None,
    spawn_timeout: float | None = None,
) -> PlacedLaunch | dict[str, Any] | None:
    """Resolve a placement before any side effect; return a refusal reply or ``None``.

    ``seat_title`` is a defined seat's definition name; it names the seat's pane: a split's
    pane title, or the initial pane of a new tab. A tab keeps its runbook title.
    """
    if placement is None:
        return None
    try:
        parsed = AgentPlacement.parse(placement).for_seat(seat_title)
    except AgentPlacementError as exc:
        return _refusal(exc.code, str(exc) or type(exc).__name__)
    try:
        await asyncio.to_thread(require_managed_srt, sandbox_config)
    except SandboxRequiredError as exc:
        return _refusal("sandbox_required", str(exc) or type(exc).__name__)
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
    wait = placement_wait_seconds(spawn_timeout)
    deadline = asyncio.timeout(wait)
    try:
        # Session reads take the default bound; the spawn's own timeout bounds only the
        # reserver's resolution, which runs after them.
        async with asyncio.timeout(PLACEMENT_WAIT_SECONDS):
            # Only a team manager's own-team seat may place from an autonomous caller: a
            # resume, or a fresh seat into its epic team's tab.
            own_seat = (
                resume_session_id is not None
                and session_manager is not None
                and await asyncio.to_thread(
                    own_seat_resume,
                    session_manager,
                    parent_session_id,
                    agent_name,
                    resume_session_id,
                )
            )
            own_tab: str | None = None
            if resume_session_id is None and session_manager is not None and agent_name is not None:
                own_tab = await asyncio.to_thread(
                    own_team_tab, session_manager, parent_session_id, agent_name
                )
        async with deadline:
            resolved = await reserver.preflight(
                f"session:{parent_session_id}",
                project_id,
                parsed,
                own_seat_resume=own_seat,
                own_team_tab=own_tab,
            )
    except AgentPlacementError as exc:
        return _refusal(exc.code, str(exc) or type(exc).__name__)
    except TimeoutError:
        # Both bounds raise the same timeout; the message names no wait since two apply.
        message = "Placed spawn timed out resolving the placement"
        logger.warning("Placed spawn refused: %s", message)
        return _refusal("timeout", message)
    return PlacedLaunch(
        reserver=reserver, resolved=resolved, replaces_tab=replaces_tab, wait_seconds=wait
    )


def task_active_refusal(response: dict[str, Any]) -> dict[str, Any]:
    """Turn the active-task skipped reply into a placed refusal; pass other refusals on."""
    if response.get("skipped") is True:
        return {
            "success": False,
            "placement_error": "task_active",
            "run_id": response.get("run_id"),
            "error": response.get("error")
            or response.get("message")
            or "Task already has an active agent run",
        }
    return response


async def run_placed_spawn(
    placed: PlacedLaunch, phase: SpawnPhase, *, worktree_id: str | None
) -> dict[str, Any]:
    """Reserve the pane, launch inline, then settle on success or release on any other exit."""
    reserver = placed.reserver
    try:
        if placed.replaces_tab is not None:
            await reserver.close_empty_tab(placed.replaces_tab)
        reserved = await reserver.reserve(
            placed.resolved, worktree_id=worktree_id, timeout=placed.wait_seconds
        )
    except asyncio.CancelledError:
        await phase.fail("Agent spawn cancelled")
        raise
    except AgentPlacementError as exc:
        return {**await phase.fail(str(exc) or type(exc).__name__), "placement_error": exc.code}
    except Exception as exc:
        return await phase.fail(str(exc) or type(exc).__name__)

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
