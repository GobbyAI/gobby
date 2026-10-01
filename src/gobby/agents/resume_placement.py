"""Re-place a resumed placed agent against current workspace state (placed-agent-launch 1.7).

The launch snapshot records the placement a spawn validated. Resume treats it as
input for a fresh preflight, reserve and bind, never as authority to skip one,
and has no unplaced fallback.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from gobby.agents import srt_runtime
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.spawn_models import SpawnRequest, SpawnResult
from gobby.terminals.workspace_agent_panes import (
    AgentPaneReserver,
    AgentPlacement,
    AgentPlacementError,
    ResolvedPlacement,
)
from gobby.utils.git import run_to_completion

if TYPE_CHECKING:
    from gobby.agents.spawn_executor_providers import ProviderSpawnPlan

RuntimeSpawn = Callable[[SpawnRequest, "ProviderSpawnPlan"], Awaitable[SpawnResult]]


def placement_snapshot(resolved: ResolvedPlacement) -> dict[str, Any]:
    """The validated placement a resume replays: kind, workspace, title and split target."""
    placement = resolved.placement
    return {
        "kind": placement.kind,
        "workspace_id": resolved.workspace_id,
        "title": placement.title,
        "beside_pane_id": resolved.beside_pane_id,
        "axis": placement.axis,
    }


def _refusal(request: SpawnRequest, code: str) -> SpawnResult:
    assert request.agent_run_id is not None
    return SpawnResult(
        success=False,
        run_id=request.agent_run_id,
        child_session_id=request.session_id,
        status="failed",
        error=f"placement_error:{code}",
    )


async def _preflight(
    snapshot: dict[str, Any],
    *,
    reserver: AgentPaneReserver | None,
    sandbox_config: SandboxConfig | None,
    sessions: Any,
    parent_session_id: str,
    project_id: str,
) -> ResolvedPlacement | str:
    """Resolve the recorded placement against current state; return it or a refusal code."""
    if reserver is None:
        return "placement_unavailable"
    if sandbox_config is None or not (sandbox_config.enabled and sandbox_config.backend == "srt"):
        return "sandbox_required"
    try:
        await asyncio.to_thread(srt_runtime.verify_srt_installation)
    except srt_runtime.SrtRuntimeError:
        return "sandbox_required"
    try:
        parent = await asyncio.to_thread(sessions.get, parent_session_id)
    except LookupError:
        parent = None
    if parent is None or getattr(parent, "machine_id", None) is None:
        return "parent_unresolved"
    kind = snapshot.get("kind")
    workspace_id = snapshot.get("workspace_id")
    ref = workspace_id if kind == "tab" else snapshot.get("beside_pane_id")
    title, axis = snapshot.get("title"), snapshot.get("axis")
    if (
        kind not in {"tab", "split"}
        or not isinstance(ref, str)
        or not isinstance(title, str)
        or (kind == "split") != isinstance(axis, str)
    ):
        return "invalid_placement"
    placement = AgentPlacement(kind=kind, ref=ref, title=title, axis=axis)
    try:
        resolved = await reserver.preflight(f"session:{parent_session_id}", project_id, placement)
    except AgentPlacementError as exc:
        return exc.code
    if resolved.workspace_id != workspace_id:
        # The split target moved to another workspace since the agent was placed.
        return "not_found"
    return resolved


async def launch_resume(
    request: SpawnRequest,
    plan: ProviderSpawnPlan,
    *,
    runtime_spawn: RuntimeSpawn,
    snapshot: dict[str, Any] | None,
    reserver: AgentPaneReserver | None,
    sessions: Any,
    parent_session_id: str,
    project_id: str,
    worktree_id: str | None,
    park: Callable[[], Awaitable[None]],
) -> SpawnResult:
    """Launch the successor; a placed one is re-placed or refused, never launched unplaced.

    A refused or failed placed launch returns a failed result for the caller to park.
    Cancellation releases the pane and parks the successor once, then re-raises.
    """
    if snapshot is None:
        return await runtime_spawn(request, plan)
    resolved = await _preflight(
        snapshot,
        reserver=reserver,
        sandbox_config=request.sandbox_config,
        sessions=sessions,
        parent_session_id=parent_session_id,
        project_id=project_id,
    )
    if isinstance(resolved, str):
        return _refusal(request, resolved)
    assert reserver is not None
    try:
        reserved = await reserver.reserve(resolved, worktree_id=worktree_id)
    except asyncio.CancelledError:
        await run_to_completion(park())
        raise
    except AgentPlacementError as exc:
        return _refusal(request, exc.code)

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

    async def release_and_park() -> None:
        await reserver.release(reserved, terminal_id=launch_terminal_id)
        await park()

    async def release_after_failure() -> None:
        # The caller parks a failure; a cancellation here skips the caller, so park now.
        try:
            await run_to_completion(reserver.release(reserved, terminal_id=launch_terminal_id))
        except asyncio.CancelledError:
            await run_to_completion(park())
            raise

    request.placement_binder = bind
    try:
        result = await runtime_spawn(request, plan)
    except asyncio.CancelledError:
        await run_to_completion(release_and_park())
        raise
    except Exception:
        await release_after_failure()
        raise
    if result.success:
        reserver.settle(reserved)
        return result
    await release_after_failure()
    if bind_error is not None:
        result.error = f"placement_error:{bind_error}"
    return result
