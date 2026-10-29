"""Epic-team spawn scope for the team's Dev Manager (redesign 8.2).

The Dev Manager holds no claim; its run is bound to the epic, and it dispatches the team's seats
here. A maker takes an open, unclaimed leaf of the epic. A primary leaf works in the team tree
and records its implementation domain for skills: only one live primary works that tree. A
parallel leaf uses a Dev Pod on ``epic/<seq>/<leaf-seq>`` and holds no primary slot.
A reviewer works in the team tree and is recorded on the team, one live seat per definition. A
resumed seat keeps the definition its session was stored under. A resumed reviewer rebinds that
definition to the resumed run, and a resumed primary takes its domain slot back. A team manager's
other fresh spawn is refused, and every other caller takes the ordinary path. Any other resumed
seat takes no scope here: spawn admission bounds it to the manager's own team's seats.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gobby.agents.isolation_models import title_slug
from gobby.agents.team_lifecycle import (
    TeamAdmissionRefused,
    TeamBinding,
    bind_primary_run,
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
from gobby.workflows.state_manager import SessionVariableManager

_LIVE = frozenset({"queued", "pending", "running"})
_MAKER_GRANT = "implementation"
_REVIEWER_GRANT = "review"
# A leaf with no implementation domain takes the fullstack primary slot.
_UNDOMAINED_DOMAIN = "fullstack"


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
    primary_domain: str | None = None


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


def _live_primary(db: HubDatabase, binding: TeamBinding) -> bool:
    """Include the initial runbook maker, which has no domain dispatch binding."""
    if any(_live(db, run_id) for run_id in binding.primaries.values()):
        return True
    rows = db.fetchall(
        "SELECT s.id FROM sessions s JOIN agent_runs r ON s.agent_run_id = r.id "
        "LEFT JOIN sessions parent ON parent.id = r.parent_session_id "
        "WHERE s.project_id = %s AND r.status = ANY(%s) "
        "AND (r.worktree_id = %s OR parent.source = 'pipeline')",
        (binding.project_id, sorted(_LIVE), binding.worktree_id),
    )
    sessions = SessionManager(db)
    for row in rows:
        team = seat_team(db, row["id"])
        if team is None or team.team_id != binding.team_id:
            continue
        session = sessions.get(row["id"])
        definition = session_agent_definition(db, session) if session is not None else None
        if definition is not None and _MAKER_GRANT in definition[0].task_step_writes:
            return True
    return False


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
    leaf = LocalTaskManager(db).get_task(leaf_id)
    if checkout_mode == "worktree":
        raise TeamAdmissionRefused(
            "team_pod_required",
            "launch the devteam pipeline with parent_team_id for a parallel leaf",
        )
    if worktree_id not in (None, binding.worktree_id):
        raise TeamAdmissionRefused(
            "team_tree_mismatch", f"team {binding.team_id} works in tree {binding.worktree_id}"
        )
    domain = leaf.implementation_domain or _UNDOMAINED_DOMAIN
    if _live_primary(db, binding):
        raise TeamAdmissionRefused(
            "primary_live", f"team {binding.team_id} already has a live primary; use a Dev Pod"
        )
    return DispatchScope(
        team_id=binding.team_id,
        project_id=binding.project_id,
        parent_session_id=parent,
        worktree_id=binding.worktree_id,
        primary_domain=domain,
    )


def _resumed_session(db: HubDatabase, project_id: str, ref: str) -> str | None:
    """The session a resume names; None when the ref names no session.

    An unknown ref falls through to the resume path, which reports it.
    """
    try:
        return SessionManager(db).resolve_session_reference(ref, project_id)
    except ValueError:
        return None


def _stored_definition(db: HubDatabase, session_id: str) -> str:
    """The definition a session was stored under."""
    stored = SessionVariableManager(db).get_variable_subset(session_id, ("_agent_type",))
    return str(stored.get("_agent_type") or "default")


def _resumed_maker(
    db: HubDatabase, binding: TeamBinding, parent: str, session_id: str | None
) -> DispatchScope | None:
    """A resumed primary takes the team's primary slot; other resumed makers take no scope.

    A run counts as a primary when it worked the team tree, even before claiming a leaf. No primary
    may still be live, regardless of its implementation domain.
    """
    session = SessionManager(db).get(session_id) if session_id is not None else None
    last = session.agent_run_id if session is not None else None
    run = LocalAgentRunManager(db).get(last) if last else None
    if run is None or run.worktree_id != binding.worktree_id:
        return None
    if _live_primary(db, binding):
        raise TeamAdmissionRefused(
            "primary_live", f"team {binding.team_id} already has a live primary; use a Dev Pod"
        )
    domain = (
        LocalTaskManager(db).get_task(run.task_id).implementation_domain if run.task_id else None
    ) or _UNDOMAINED_DOMAIN
    return DispatchScope(
        team_id=binding.team_id,
        project_id=binding.project_id,
        parent_session_id=parent,
        primary_domain=domain,
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
    resume_session_id: str | None,
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
        worktree_id=None if resume_session_id is not None else binding.worktree_id,
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
        and definition[0].name != "researcher"
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
    resume_session_id: str | None = None,
) -> DispatchScope | None:
    """The scope a spawn from an epic team's Dev Manager launches under, or None to fall through.

    Only the epic's bound Dev Manager dispatches, and it holds no claim. A fresh spawn from any
    other team manager is refused, since a single-task team's runbook starts its seats. A resumed
    seat must keep its stored definition. A resumed reviewer is checked against the team's live
    seats and rebinds; a resumed primary takes its domain slot; any other resumed seat falls
    through. A team's leaf seats and every other caller take the ordinary path.
    """
    resumed = resume_session_id is not None
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
    session_id = (
        _resumed_session(db, binding.project_id, resume_session_id)
        if resume_session_id is not None
        else None
    )
    if session_id is not None:
        stored = _stored_definition(db, session_id)
        if stored != agent:
            raise TeamAdmissionRefused(
                "seat_definition_mismatch",
                f"seat {resume_session_id} is {stored}; it resumes as {stored}, not {agent}",
            )
        resumed_team = seat_team(db, session_id)
        if resumed_team is not None and resumed_team.parent_team_id == binding.team_id:
            if (
                task_id is not None
                or checkout_mode is not None
                or branch_name is not None
                or base_branch is not None
                or worktree_id not in (None, resumed_team.worktree_id)
            ):
                raise TeamAdmissionRefused("team_tree_fixed", "a resumed pod seat keeps its tree")
            if _REVIEWER_GRANT in grants:
                return _reviewer(
                    db, resumed_team, agent, parent, resume_session_id=resume_session_id, **options
                )
            return DispatchScope(
                team_id=resumed_team.team_id,
                project_id=resumed_team.project_id,
                parent_session_id=parent,
            )
    if _REVIEWER_GRANT in grants:
        return _reviewer(db, binding, agent, parent, resume_session_id=resume_session_id, **options)
    if agent in {"test-writer", "researcher"}:
        if (
            task_id is not None
            and open_leaf_below(db, epic.id, _leaf_id(db, binding.project_id, task_id)) is None
        ):
            raise TeamAdmissionRefused("task_outside_epic", "the seat must work an open epic leaf")
        if (
            branch_name
            or base_branch
            or checkout_mode not in (None, "none")
            or worktree_id not in (None, binding.worktree_id)
        ):
            raise TeamAdmissionRefused("team_tree_fixed", "use a Dev Pod for a parallel leaf")
        return DispatchScope(
            team_id=binding.team_id,
            project_id=binding.project_id,
            parent_session_id=parent,
            worktree_id=binding.worktree_id if agent == "test-writer" and not resumed else None,
        )
    if resumed:
        return _resumed_maker(db, binding, parent, session_id) if _MAKER_GRANT in grants else None
    if _MAKER_GRANT in grants:
        return _maker(db, binding, epic, parent, **options)
    raise TeamAdmissionRefused(
        "seat_not_granted",
        f"{agent} holds no implementation or review grant for team {binding.team_id}",
    )


def record_dispatch(db: HubDatabase, scope: DispatchScope, result: dict[str, Any]) -> None:
    """Bind a launched seat's run: a reviewer to its definition, a primary to its domain.

    The live check in ``dispatch_for_caller`` and this write are not atomic. One Dev Manager
    dispatches a team's seats, so the window has a single writer. A sub-worktree maker needs no
    record.
    """
    run_id = result.get("run_id")
    if not result.get("success") or not run_id:
        return
    if scope.reviewer_agent is not None:
        bind_reviewer_run(db, scope.team_id, scope.project_id, scope.reviewer_agent, str(run_id))
    if scope.primary_domain is not None:
        bind_primary_run(db, scope.team_id, scope.project_id, scope.primary_domain, str(run_id))
