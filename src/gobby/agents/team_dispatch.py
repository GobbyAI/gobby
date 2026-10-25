"""Epic-team spawn scope for the team's Dev Manager (redesign 8.2).

The Dev Manager holds no claim; its run is bound to the epic, and it dispatches the team's seats
here. A maker takes an open, unclaimed leaf of the epic: a primary leaf works in the team tree,
and a sub-worktree leaf gets ``epic/<seq>/<leaf-seq>`` off the epic branch. A reviewer works in the team tree and is
recorded on the team, one live seat per definition. A resumed reviewer is held to the same check
and rebinds its definition to the resumed run. A team manager's other fresh spawn is refused, and
every other caller takes the ordinary path. A resumed maker or other seat takes no scope here:
spawn admission bounds it to the manager's own team's seats.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gobby.agents.isolation_models import title_slug
from gobby.agents.team_lifecycle import (
    TeamAdmissionRefused,
    TeamBinding,
    bind_reviewer_run,
    seat_team,
)
from gobby.agents.team_review import sync_epic_review
from gobby.agents.team_scope import open_leaf_below
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.storage.team_records import EPIC_MODE
from gobby.tasks.lifecycle_access import session_agent_definition
from gobby.workflows.definitions import AgentDefinitionBody

_LIVE = frozenset({"queued", "pending", "running"})
_MAKER_GRANT = "implementation"
_REVIEWER_GRANT = "review"


@dataclass(frozen=True)
class DispatchScope:
    """The launch a Dev Manager's spawn takes inside its epic team."""

    team_id: str
    project_id: str
    parent_session_id: str
    worktree_id: str | None = None
    branch_name: str | None = None
    base_branch: str | None = None
    worktree_dir_name: str | None = None
    reviewer_agent: str | None = None


def leaf_names(epic: Task, leaf: Task) -> tuple[str, str]:
    """The branch and worktree directory of a sub-worktree leaf under its epic."""
    slug = title_slug(leaf.title)
    directory = f"wt-epic-{epic.seq_num}-{leaf.seq_num}" + (f"-{slug}" if slug else "")
    return f"epic/{epic.seq_num}/{leaf.seq_num}", directory


def _live(db: HubDatabase, run_id: str | None) -> bool:
    if run_id is None:
        return False
    run = LocalAgentRunManager(db).get(run_id)
    return run is not None and run.status in _LIVE


def _leaf_id(db: HubDatabase, project_id: str, task_ref: str) -> str:
    try:
        return LocalTaskManager(db).resolve_task_reference(task_ref, project_id)
    except (ValueError, TaskNotFoundError) as exc:
        raise TeamAdmissionRefused(
            "task_outside_epic", f"task {task_ref} does not resolve"
        ) from exc


def _maker(
    db: HubDatabase,
    binding: TeamBinding,
    epic: Task,
    parent: str,
    *,
    task_id: str | None,
    checkout_mode: str | None,
    branch_name: str | None,
    base_branch: str | None,
    worktree_id: str | None,
) -> DispatchScope:
    if task_id is None:
        raise TeamAdmissionRefused("epic_task_required", "a maker names the leaf task it takes")
    if branch_name is not None or base_branch is not None or checkout_mode == "clone":
        raise TeamAdmissionRefused(
            "team_tree_fixed", "team seats join the team tree; they cannot name a branch or clone"
        )
    leaf_id = _leaf_id(db, epic.project_id, task_id)
    open_leaf = open_leaf_below(db, epic.id, leaf_id)
    if open_leaf is None:
        raise TeamAdmissionRefused(
            "task_outside_epic", f"task {task_id} is not an open leaf of epic {epic.id}"
        )
    if open_leaf.claimed_by_session_id is not None:
        raise TeamAdmissionRefused("task_in_flight", f"task {task_id} already has a claimed seat")
    if checkout_mode == "worktree":
        if worktree_id is not None:
            raise TeamAdmissionRefused("team_tree_fixed", "a sub-worktree leaf names no tree")
        branch, directory = leaf_names(epic, LocalTaskManager(db).get_task(leaf_id))
        return DispatchScope(
            team_id=binding.team_id,
            project_id=binding.project_id,
            parent_session_id=parent,
            branch_name=branch,
            base_branch=binding.branch,
            worktree_dir_name=directory,
        )
    if worktree_id not in (None, binding.worktree_id):
        raise TeamAdmissionRefused(
            "team_tree_mismatch", f"team {binding.team_id} works in tree {binding.worktree_id}"
        )
    return DispatchScope(
        team_id=binding.team_id,
        project_id=binding.project_id,
        parent_session_id=parent,
        worktree_id=binding.worktree_id,
    )


def _reviewer(
    db: HubDatabase,
    binding: TeamBinding,
    agent: str,
    parent: str,
    *,
    task_id: str | None,
    checkout_mode: str | None,
    branch_name: str | None,
    base_branch: str | None,
    worktree_id: str | None,
    resumed: bool,
) -> DispatchScope:
    if task_id is not None:
        raise TeamAdmissionRefused(
            "reviewer_claim_forbidden", "reviewers work the team tree and never claim a task"
        )
    if checkout_mode is not None or branch_name is not None or base_branch is not None:
        raise TeamAdmissionRefused(
            "team_tree_fixed", "reviewers join the team tree; they cannot create a branch or clone"
        )
    if worktree_id not in (None, binding.worktree_id):
        raise TeamAdmissionRefused(
            "team_tree_mismatch", f"team {binding.team_id} works in tree {binding.worktree_id}"
        )
    if _live(db, binding.reviewers.get(agent)):
        raise TeamAdmissionRefused(
            "reviewer_seat_live", f"{agent} is already live on team {binding.team_id}"
        )
    # A resumed seat reuses the tree it recorded, which is the team tree for a reviewer.
    return DispatchScope(
        team_id=binding.team_id,
        project_id=binding.project_id,
        parent_session_id=parent,
        worktree_id=None if resumed else binding.worktree_id,
        reviewer_agent=agent,
    )


def _is_bound_dev_manager(db: HubDatabase, caller_session_id: str, epic_id: str) -> bool:
    """The caller's run is bound to the epic, and its definition writes the closing record."""
    caller = SessionManager(db).get(caller_session_id)
    if caller is None:
        return False
    run = LocalAgentRunManager(db).get_by_session(caller.id)
    definition = session_agent_definition(db, caller)
    return (
        run is not None
        and run.task_id == epic_id
        and definition is not None
        and "closing" in definition[0].task_step_writes
    )


def _is_team_manager(db: HubDatabase, caller_session_id: str) -> bool:
    """The caller runs an enabled definition that sets the ``team_manager`` variable."""
    caller = SessionManager(db).get(caller_session_id)
    definition = session_agent_definition(db, caller) if caller is not None else None
    return definition is not None and definition[0].workflows.variables.get("team_manager") is True


def dispatch_for_caller(
    db: HubDatabase,
    caller_session_id: str,
    *,
    agent_body: AgentDefinitionBody | None,
    agent: str,
    task_id: str | None,
    checkout_mode: str | None,
    branch_name: str | None,
    base_branch: str | None,
    worktree_id: str | None,
    resumed: bool = False,
) -> DispatchScope | None:
    """The scope a spawn from an epic team's Dev Manager launches under, or None to fall through.

    Only the epic's bound Dev Manager dispatches, and it holds no claim. A fresh spawn from any
    other team manager is refused, since a single-task team's runbook starts its seats. A resume
    never refuses for scope: a reviewer is checked against the team's live seats and rebinds, and
    any other resumed seat falls through. A team's leaf seats and every other caller take the
    ordinary path.
    """
    binding = seat_team(db, caller_session_id)
    if (
        binding is None
        or binding.mode != EPIC_MODE
        or not _is_bound_dev_manager(db, caller_session_id, binding.task_id)
    ):
        if not resumed and _is_team_manager(db, caller_session_id):
            raise TeamAdmissionRefused(
                "team_manager_scope",
                "a team manager spawns only inside its epic team; a single-task team's runbook "
                "starts its seats",
            )
        return None
    epic = LocalTaskManager(db).get_task(binding.task_id)
    if sync_epic_review(db, binding.team_id).held:
        raise TeamAdmissionRefused("team_held", f"team {binding.team_id} is held for review")
    caller = SessionManager(db).get(caller_session_id)
    parent = caller.id if caller is not None else caller_session_id
    grants = set(agent_body.task_step_writes) if agent_body is not None else set()
    options: dict[str, Any] = {
        "task_id": task_id,
        "checkout_mode": checkout_mode,
        "branch_name": branch_name,
        "base_branch": base_branch,
        "worktree_id": worktree_id,
    }
    if _REVIEWER_GRANT in grants:
        return _reviewer(db, binding, agent, parent, resumed=resumed, **options)
    if resumed:
        return None
    if _MAKER_GRANT in grants:
        return _maker(db, binding, epic, parent, **options)
    raise TeamAdmissionRefused(
        "seat_not_granted",
        f"{agent} holds no implementation or review grant for team {binding.team_id}",
    )


def record_dispatch(db: HubDatabase, scope: DispatchScope, result: dict[str, Any]) -> None:
    """Bind a launched reviewer's run to its definition on the team; makers need no record.

    The live check in ``dispatch_for_caller`` and this write are not atomic. One Dev Manager
    dispatches a team's seats, so the window has a single writer.
    """
    run_id = result.get("run_id")
    if scope.reviewer_agent is None or not result.get("success") or not run_id:
        return
    bind_reviewer_run(db, scope.team_id, scope.project_id, scope.reviewer_agent, str(run_id))
