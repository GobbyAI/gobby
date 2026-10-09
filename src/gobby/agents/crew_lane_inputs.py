"""Read-only resolution of the project crew-lane runbook's launch inputs."""

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from gobby.agents.runbook_seats import READ_BOUND, RunbookSeatRefusal
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import require_root
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import LIVE_SESSION_STATUS_ORDER, LIVE_SESSION_STATUSES
from gobby.storage.workspace_layout import (
    MIN_PANE_COLUMNS,
    MIN_PANE_ROWS,
    WorkspaceNotFoundError,
    layout_extent,
)
from gobby.storage.workspaces import Workspace, WorkspaceManager, WorkspaceTarget
from gobby.storage.worktrees import LocalWorktreeManager

# The distinct (rows, cols) live gclient windows show a terminal at.
type Viewports = Callable[[str], Collection[tuple[int, int]]]


@dataclass(frozen=True)
class CrewLaneInputs:
    workspace: str
    lane_pane: str
    worktree: str
    report_to: str
    project_path: str
    lane_columns: int
    lane_rows: int


def _refuse(kind: str, candidates: list[str]) -> RunbookSeatRefusal:
    return RunbookSeatRefusal(
        f"{kind} must resolve to exactly one candidate; candidates: "
        + (", ".join(sorted(candidates)) or "none")
    )


def _manager(
    sessions: SessionManager, caller: Session, root: Path, lane: int, report_to: str | None
) -> Session:
    if report_to is None:
        try:
            roster = (root / ".gobby/roles/roster.md").read_text(encoding="utf-8")
        except OSError as exc:
            raise _refuse("manager", []) from exc
        refs = re.findall(r"^\|\s*lane-manager\.md\s*\|\s*([^|]+)\|", roster, re.M)
        refs = [ref.strip() for ref in refs]
        # Manager rows are in lane order; additional lanes name their manager in prose.
        if len(refs) != 7 or len(set(refs)) != 7:
            raise _refuse("manager", refs)
        assignments = {
            int(match.group(2))
            for match in re.finditer(
                r"\b(?:Lane\s+)?([1-9])\b(?:(?!\b[1-9]\b)[^\n])*?"
                r"\bmanaged by the Lane ([1-9]) manager",
                roster,
            )
            if int(match.group(1)) == lane
        }
        if len(assignments) > 1:
            raise _refuse(f"Lane {lane} manager", [f"Lane {n}" for n in assignments])
        manager_lane = next(iter(assignments), lane)
        if manager_lane > len(refs):
            raise RunbookSeatRefusal(
                f"Lane {lane} has no manager row or parsable managed-by assignment in the roster"
            )
        report_to = refs[manager_lane - 1]
    try:
        manager_id = sessions.resolve_session_reference(report_to, project_id=caller.project_id)
        manager = sessions.get(manager_id)
    except ValueError as exc:
        raise _refuse("manager", [report_to]) from exc
    if (
        manager is None
        or manager.project_id != caller.project_id
        or manager.machine_id != caller.machine_id
        or manager.status not in LIVE_SESSION_STATUSES
    ):
        raise _refuse("manager", [report_to])
    return manager


def _manager_pane(workspaces: WorkspaceManager, manager: Session) -> WorkspaceTarget | None:
    context = manager.terminal_context or {}
    pane_ref = context.get("gobby_pane_ref") or context.get("pane_ref")
    if not pane_ref:
        terminal = context.get("gobby_terminal_id")
        pane = workspaces.get_pane_for_terminal(terminal) if isinstance(terminal, str) else None
        pane_ref = pane.id if pane else None
    if not isinstance(pane_ref, str):
        return None
    try:
        target = workspaces.resolve_reference(pane_ref, node=manager.machine_id)
    except (ValueError, WorkspaceNotFoundError):
        return None
    if (
        target.pane is None
        or target.tab is None
        or target.tab.project_id != manager.project_id
        or target.node.id != manager.machine_id
    ):
        return None
    return target


def _viewport_refusal(sizes: Collection[tuple[int, int]]) -> RunbookSeatRefusal:
    refusal = _refuse("lane viewport", [f"{columns}x{rows}" for rows, columns in sizes])
    return RunbookSeatRefusal(
        f"{refusal}; show the lane's workspace in gclient or pass lane_rows and lane_columns"
    )


def _lane_viewport(
    workspaces: WorkspaceManager, workspace: Workspace, viewports: Viewports | None
) -> tuple[int, int]:
    """Return the (columns, rows) of the gclient window showing ``workspace``.

    Every tab in a window shares its content area, and only the shown tab's panes
    carry live sizes: a hidden tab keeps the sizes it was last shown at.
    """
    shown_tab = next(
        (tab for tab in workspaces.list_tabs(workspace.id) if tab.id == workspace.focused_tab_id),
        None,
    )
    if shown_tab is None or viewports is None:
        raise _viewport_refusal([])
    sizes: dict[str, tuple[int, int]] = {}
    for pane in workspaces.list_panes(workspace.id):
        if pane.tab_id != shown_tab.id:
            continue
        shown = viewports(pane.terminal_id) if pane.terminal_id else ()
        if len(shown) != 1:
            raise _viewport_refusal(shown)
        [(rows, columns)] = shown
        sizes[pane.id] = (columns, rows)
    return layout_extent(shown_tab.layout, sizes)


def resolve_crew_lane_inputs(
    db: HubDatabase,
    *,
    caller_session_id: str,
    lane: str | int,
    workspace: str | None = None,
    lane_pane: str | None = None,
    worktree: str | None = None,
    report_to: str | None = None,
    lane_columns: int | None = None,
    lane_rows: int | None = None,
    viewports: Viewports | None = None,
) -> CrewLaneInputs:
    """Resolve overrides or unique existing lane state, without creating or claiming anything."""
    lane = str(lane)
    if not re.fullmatch(r"[1-9]", lane):
        raise RunbookSeatRefusal("lane must be a number from 1 to 9")
    caller = SessionManager(db).get(caller_session_id)
    if caller is None:
        raise RunbookSeatRefusal("crew-lane needs a caller session")
    root = Path(require_root(db, caller.project_id, caller.machine_id))
    sessions = SessionManager(db)
    manager = _manager(sessions, caller, root, int(lane), report_to)
    workspaces = WorkspaceManager(db)
    manager_pane = _manager_pane(workspaces, manager)
    tabs = [
        tab
        for candidate in workspaces.list_for_node(caller.machine_id)
        for tab in workspaces.list_tabs(candidate.id)
        if tab.project_id == caller.project_id
        and re.fullmatch(rf"Lane {lane}(?:\s+[-–—:·].*)?", tab.title or "")
    ]
    try:
        explicit_pane = (
            workspaces.resolve_reference(lane_pane, node=caller.machine_id)
            if lane_pane is not None
            else None
        )
    except (ValueError, WorkspaceNotFoundError) as exc:
        raise _refuse(
            "pane",
            sorted(
                {
                    pane.id
                    for tab in tabs
                    for pane in workspaces.list_panes(tab.workspace_id)
                    if pane.tab_id == tab.id
                }
            ),
        ) from exc
    if workspace is not None:
        try:
            target = workspaces.resolve_reference(workspace, node=caller.machine_id)
        except (ValueError, WorkspaceNotFoundError) as exc:
            raise _refuse(
                "workspace",
                [
                    f"{candidate.name} ({candidate.id})"
                    for candidate in workspaces.list_for_node(caller.machine_id)
                ],
            ) from exc
    elif explicit_pane is not None:
        target = explicit_pane
    else:
        ids = {tab.workspace_id for tab in tabs}
        if not ids and manager_pane:
            ids.add(manager_pane.workspace.id)
        if len(ids) != 1:
            raise _refuse("workspace", sorted(ids))
        target = workspaces.resolve_reference(ids.pop(), node=caller.machine_id)
    if target.node.id != caller.machine_id:
        raise RunbookSeatRefusal("workspace belongs to another machine")
    tabs = [tab for tab in tabs if tab.workspace_id == target.workspace.id]
    if explicit_pane is not None:
        if (
            explicit_pane.pane is None
            or explicit_pane.tab is None
            or explicit_pane.tab.project_id != caller.project_id
            or explicit_pane.workspace.id != target.workspace.id
            or explicit_pane.node.id != caller.machine_id
        ):
            raise RunbookSeatRefusal("lane_pane does not belong to the target workspace/project")
        pane_id = explicit_pane.pane.id
    elif (
        manager_pane is not None
        and manager_pane.tab is not None
        and manager_pane.workspace.id == target.workspace.id
        and (not tabs or any(tab.id == manager_pane.tab.id for tab in tabs))
    ):
        assert manager_pane.pane is not None
        pane_id = manager_pane.pane.id
    else:
        tab_ids = {tab.id for tab in tabs}
        panes = [
            pane for pane in workspaces.list_panes(target.workspace.id) if pane.tab_id in tab_ids
        ]
        if len(panes) != 1:
            raise _refuse("pane", [pane.id for pane in panes])
        pane_id = panes[0].id
    if lane_columns is None or lane_rows is None:
        columns, rows = _lane_viewport(workspaces, target.workspace, viewports)
        lane_columns = columns if lane_columns is None else lane_columns
        lane_rows = rows if lane_rows is None else lane_rows
    if lane_columns < MIN_PANE_COLUMNS or lane_rows < MIN_PANE_ROWS:
        raise RunbookSeatRefusal(
            f"lane viewport {lane_columns}x{lane_rows} is under the "
            f"{MIN_PANE_COLUMNS}x{MIN_PANE_ROWS} pane floor"
        )

    trees = LocalWorktreeManager(db).list_worktrees(
        caller.project_id, status="active", limit=READ_BOUND + 1
    )
    if len(trees) > READ_BOUND:
        raise RunbookSeatRefusal("worktree candidates exceed the read bound")
    trees = [tree for tree in trees if tree.machine_id == caller.machine_id]
    if worktree is not None:
        matches = [tree for tree in trees if worktree in (tree.id, tree.branch_name)]
        if len(matches) != 1:
            raise _refuse("worktree", [f"{tree.branch_name} ({tree.id})" for tree in trees])
    else:
        pattern = re.compile(rf"lane-{lane}(?:$|[-/])")
        associated = {tab.worktree_id for tab in tabs if tab.worktree_id}
        candidates = [
            tree
            for tree in trees
            if tree.id in associated
            or pattern.match(tree.branch_name or "")
            or pattern.match(Path(tree.worktree_path).name)
        ]
        live = sessions.list(
            project_id=caller.project_id,
            machine_id=caller.machine_id,
            statuses=LIVE_SESSION_STATUS_ORDER,
            limit=READ_BOUND + 1,
        )
        if len(live) > READ_BOUND:
            raise RunbookSeatRefusal("live seat candidates exceed the read bound")
        live_ids = {session.id for session in live}
        live_paths = {
            Path(session.workspace_path).resolve() for session in live if session.workspace_path
        }
        runs = LocalAgentRunManager(db)
        matches = [
            tree
            for tree in candidates
            if tree.agent_session_id not in live_ids
            and Path(tree.worktree_path).resolve() not in live_paths
            and runs.get_active_run_for_worktree(tree.id) is None
        ]
        if len(matches) != 1:
            raise _refuse("worktree", [f"{tree.branch_name} ({tree.id})" for tree in candidates])
    chosen = matches[0]
    if not Path(chosen.worktree_path).is_dir():
        raise RunbookSeatRefusal(f"worktree path does not exist: {chosen.worktree_path}")
    return CrewLaneInputs(
        target.workspace.id,
        pane_id,
        chosen.id,
        manager.ref,
        str(root),
        lane_columns,
        lane_rows,
    )
