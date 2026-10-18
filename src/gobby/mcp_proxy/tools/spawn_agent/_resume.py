"""Validate an explicit native-thread relaunch before spawn allocations."""

import asyncio
from pathlib import Path
from typing import Any

from gobby.agents.resume_executor import SUPPORTED_RESUME_PROVIDERS
from gobby.agents.resume_placement import split_target
from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES, AgentRun
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import LIVE_SESSION_STATUSES
from gobby.storage.workspaces import WorkspaceManager
from gobby.utils.project_context import get_project_context
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.state_manager import SessionVariableManager

from ._provider_resolution import concrete_provider

_RETYPE_REFUSED = frozenset({"assistant", "orchestrator", "default"})
# Bounds the resume-chain walk for a placement recorded before resumes dropped it.
_RESUME_CHAIN_LIMIT = 32
# Recorded split axes back to the ``placement`` input spelling.
_INPUT_AXES = {"horizontal": "right", "vertical": "down", "balanced": "balanced"}


def _paused_seat_has_live_pane_or_run(
    session: Session, db: Any, run_storage: Any, live_host_epoch: str | None
) -> bool:
    """Whether a paused seat is still live: a live managed pane or an active run.

    A native pane is live only under the running host's epoch. A row stamped with
    another epoch, or a pending row (which never carries an epoch), belongs to a host
    that stopped, so it does not hold the seat. An unknown epoch keeps every native
    row holding it. Tmux panes survive a host respawn, so they count by state alone.
    A paused seat whose pane is gone and whose run is cancelled, failed or absent
    is a dead seat, and it must stay resumable.
    """
    pane_rows = db.fetchall(
        """
        SELECT 1 FROM terminals
        WHERE session_id = %s
          AND state IN ('pending', 'live')
          AND (%s::text IS NULL OR backend <> 'native' OR host_epoch = %s)
        LIMIT 1
        """,
        (session.id, live_host_epoch, live_host_epoch),
    )
    if pane_rows:
        return True
    if session.agent_run_id is None:
        return False
    run = run_storage.get(session.agent_run_id)
    return run is not None and run.status in ACTIVE_AGENT_RUN_STATUSES


def resolve_resume_target(
    session_manager: SessionManager | None,
    ref: str,
    project_id: str | None,
    provider: str | None,
    run_storage: Any,
    agent_type: str,
    lane: str | None = None,
    *,
    live_host_epoch: str | None = None,
) -> Session:
    if session_manager is None or not project_id:
        raise ValueError("Resume requires a session manager and project context")
    session_id = session_manager.resolve_session_reference(ref, project_id)
    session = session_manager.get(session_id)
    if session is None:
        raise ValueError(f"Resume target {ref} does not exist")
    if session.project_id != project_id:
        raise ValueError(f"Resume target {ref} belongs to another project")
    if session.source not in SUPPORTED_RESUME_PROVIDERS:
        raise ValueError(f"Provider {session.source!r} does not support native resume")
    if provider and provider != session.source:
        raise ValueError(f"Resume target uses {session.source}, not requested provider {provider}")
    if session.status in LIVE_SESSION_STATUSES and (
        session.status != "paused"
        or _paused_seat_has_live_pane_or_run(
            session, session_manager.db, run_storage, live_host_epoch
        )
    ):
        raise ValueError(f"Resume target {ref} is still live ({session.status})")
    if session.agent_run_id:
        run = run_storage.get(session.agent_run_id)
        if run is not None and run.status in ACTIVE_AGENT_RUN_STATUSES:
            raise ValueError(f"Resume target {ref} still has a live agent run ({run.status})")
    if session.status == "deleted":
        raise ValueError(f"Resume target {ref} is deleted")
    thread = session.external_id
    if (
        not thread
        or thread.startswith("agent-")
        or (thread == session.id and session.source != "claude")
    ):
        raise ValueError(f"Resume target {ref} has no recorded provider thread")
    # A resume merges the requested definition's variables into the session, so it
    # re-types the seat. Moving onto another seat definition stays open; becoming the
    # Assistant or Orchestrator would bypass seat rules, and the 'default' an omitted
    # agent resolves to would silently drop the seat's definition.
    stored_type = (
        SessionVariableManager(session_manager.db)
        .get_variable_subset(session.id, ("_agent_type",))
        .get("_agent_type")
    ) or "default"
    if agent_type != stored_type and agent_type in _RETYPE_REFUSED:
        raise ValueError(
            f"Resume target {ref} is a {stored_type!r} session; resume it with "
            f"agent={stored_type!r}, not {agent_type!r}"
        )
    # A resume keeps the seat in the lane it was recorded in, for every caller.
    recorded_lane = resumed_seat_lane(session, run_storage)
    requested_lane = (lane or "").strip()
    if requested_lane and recorded_lane and requested_lane != recorded_lane:
        raise ValueError(
            f"Resume target {ref} is in lane {recorded_lane!r}; it cannot resume into "
            f"{requested_lane!r}"
        )
    return session


async def resolve_seat_resume(
    session_manager: SessionManager | None,
    resume_session_id: str,
    target_project_id: str | None,
    project_path: str | None,
    provider: str | None,
    run_storage: Any,
    agent_body: AgentDefinitionBody | None,
    lane: str | None,
    *,
    live_host_epoch: str | None = None,
) -> Session:
    """Resolve the stored seat a resume reference names, in the target or checkout project."""
    project_id = target_project_id
    if project_id is None:
        project_ctx = await asyncio.to_thread(
            get_project_context, Path(project_path) if project_path else None
        )
        project_id = (
            (project_ctx.get("id") or project_ctx.get("project_id")) if project_ctx else None
        )
    return await asyncio.to_thread(
        resolve_resume_target,
        session_manager,
        resume_session_id,
        project_id,
        concrete_provider(provider),
        run_storage,
        agent_body.name if agent_body is not None else "default",
        lane,
        live_host_epoch=live_host_epoch,
    )


class ResumeSeatGone(LookupError):
    """Raised only when a placed seat records no workspace to return to."""


def _recorded_placement(run_storage: Any, run: AgentRun) -> dict[str, Any] | None:
    """The newest placement recorded along the seat's resume chain, newest run first."""
    seen: set[str] = set()
    current: AgentRun | None = run
    while current is not None and current.id not in seen and len(seen) < _RESUME_CHAIN_LIMIT:
        seen.add(current.id)
        metadata = current.resume_metadata_json or {}
        if isinstance(metadata.get("placement"), dict):
            return dict(metadata["placement"])
        previous = metadata.get("resume_previous_run_id")
        current = run_storage.get(previous) if isinstance(previous, str) else None
    return None


def _empty_tab(workspaces: WorkspaceManager, workspace_id: str, tab_id: object) -> str | None:
    """The recorded tab's id when it sits in the workspace and holds no panes."""
    if not isinstance(tab_id, str):
        return None
    if not any(tab.id == tab_id for tab in workspaces.list_tabs(workspace_id)):
        return None
    if any(pane.tab_id == tab_id for pane in workspaces.list_panes(workspace_id)):
        return None
    return tab_id


def resumed_seat_placement(
    session: Session, run_storage: Any, workspaces: WorkspaceManager
) -> dict[str, Any] | None:
    """The ``placement`` input that returns a resumed seat to its pane's tab."""
    return resumed_seat_plan(session, run_storage, workspaces)[0]


def resumed_seat_plan(
    session: Session, run_storage: Any, workspaces: WorkspaceManager
) -> tuple[dict[str, Any] | None, str | None]:
    """The ``placement`` input for a resumed seat, and the empty tab it replaces.

    A resumed seat returns to its lane tab; only a never-placed seat launches unplaced.
    The previous run's own pane comes first, even dead: the successor splits in beside
    it and inherits its slot when the sweep collapses it. Otherwise the newest placement
    recorded along the resume chain names the split target, a pane left in its tab, or
    the tab a tab-kind seat opened. None means the seat was never placed. The second
    element names the recorded tab when it holds no panes; the launch closes it before
    the reopened tab takes its title, so the seat never ends with two tabs of one name.
    """
    run = run_storage.get(session.agent_run_id) if session.agent_run_id else None
    if run is None:
        return None, None
    snapshot = _recorded_placement(run_storage, run)
    recorded_title = snapshot.get("title") if snapshot else None
    own = workspaces.get_pane_for_terminal(run.terminal_id) if run.terminal_id else None
    if own is not None:
        title = own.label or recorded_title or session.title or run.agent_name or "agent"
        return {"split": {"pane": own.id, "axis": "down", "title": title}}, None
    if snapshot is None or not isinstance(recorded_title, str):
        return None, None
    workspace_id = snapshot.get("workspace_id")
    if snapshot.get("kind") == "tab" and isinstance(workspace_id, str):
        tab = next(
            (tab for tab in workspaces.list_tabs(workspace_id) if tab.title == recorded_title),
            None,
        )
        if tab is None:
            return {"tab": {"workspace": workspace_id, "title": recorded_title}}, None
        snapshot = {"workspace_id": workspace_id, "tab_id": tab.id, "axis": "vertical"}
    target = split_target(workspaces, snapshot)
    if target is None and isinstance(workspace_id, str):
        # The seat's pane and tab are both gone, but its workspace is known: reopen a tab.
        empty = _empty_tab(workspaces, workspace_id, snapshot.get("tab_id"))
        return {"tab": {"workspace": workspace_id, "title": recorded_title}}, empty
    if target is None:
        raise ResumeSeatGone(
            f"Resume target {session.id} has no recorded workspace to return to; "
            "pass placement to seat it"
        )
    axis = snapshot.get("axis")
    split: dict[str, Any] = {
        "pane": target,
        "axis": _INPUT_AXES.get(axis, "down") if isinstance(axis, str) else "down",
        "title": recorded_title,
    }
    if axis == "balanced":
        split |= {"columns": snapshot.get("columns", 80), "rows": snapshot.get("rows")}
    return {"split": split}, None


def resume_isolation(session: Session, run_storage: Any) -> tuple[str | None, str | None]:
    """Return the worktree and clone ids the resumed session's last run worked in."""
    run = run_storage.get(session.agent_run_id) if session.agent_run_id else None
    if run is None:
        return None, None
    return run.worktree_id, run.clone_id


def resumed_seat_lane(session: Session | None, run_storage: Any) -> str | None:
    """The lane the seat's last run recorded, so a resume keeps the seat in its lane."""
    if session is None or session.agent_run_id is None:
        return None
    run = run_storage.get(session.agent_run_id)
    lane = (run.resume_metadata_json or {}).get("lane") if run is not None else None
    return lane if isinstance(lane, str) and lane else None
