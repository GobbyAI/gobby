"""Seats of an admitted team and the daemon pieces its teardown pass uses (plan 3.3)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from gobby.agents.recovery_state import is_daemon_stop_parked
from gobby.agents.team_lifecycle import TeamBinding
from gobby.agents.team_teardown import TEARDOWN_KEY, TeamTreeDeleter, reconcile_team_teardowns
from gobby.runner_maintenance.isolation import team_run_terminator, team_tree_deleter
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.daemon_resume_keys import REAP_REQUESTED_AT_KEY
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from tests.fixtures.landing_case import bind_task_manager
from tests.fixtures.team_case import TeamWorld

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

ROLES = ("test-writer", "developer", "code-reviewer")


@dataclass
class Team:
    task: Task
    runbook: Session
    binding: TeamBinding
    seats: list[str]


@dataclass
class Terminations:
    """What the daemon termination leaves behind: the run cancelled, its session expired.

    ``stop_after`` cancels the pass, as a daemon shutdown does, once that many seats ended.
    """

    db: HubDatabase
    calls: list[dict[str, Any]] = field(default_factory=list)
    stop_after: int | None = None

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        if self.stop_after is not None and len(self.calls) >= self.stop_after:
            raise asyncio.CancelledError
        self.calls.append(kwargs)
        run: AgentRun = kwargs["run"]
        runs = LocalAgentRunManager(self.db)
        if is_daemon_stop_parked(run):
            # Ending a parked seat hands its original to the orphan reaper, as the daemon does.
            runs.merge_resume_metadata(
                run.id, {REAP_REQUESTED_AT_KEY: datetime.now(UTC).isoformat()}
            )
        runs.cancel(run.id)
        if run.child_session_id is not None:
            SessionManager(self.db).update_status(run.child_session_id, "expired")
        return {"success": True}

    def ended(self) -> list[str]:
        return sorted(str(call["run"].child_session_id) for call in self.calls)


def seat(
    world: TeamWorld,
    runbook: Session,
    binding: TeamBinding,
    role: str,
    *,
    manages: Task | None = None,
) -> str:
    """A live seat the runbook launched, working in the team tree."""
    db = world.case.db
    session = world.case.session(role, parent=runbook.id)
    db.execute(
        "UPDATE sessions SET workspace_path = %s WHERE id = %s", (binding.worktree_path, session)
    )
    if manages is not None:
        bind_task_manager(
            db, manager=session, parent=runbook.id, project_id=world.case.project_id, task=manages
        )
    else:
        LocalAgentRunManager(db).create(
            parent_session_id=runbook.id,
            provider="claude",
            prompt=f"Run the {role} seat.",
            agent_name=role,
            child_session_id=session,
        )
    return session


def dispatched_seat(world: TeamWorld, team: Team, tree: str) -> str:
    """A seat the Dev Manager dispatched into ``tree``, a child of its session, not the runbook's."""
    db = world.case.db
    manager = team.seats[0]
    session = world.case.session("developer", parent=manager)
    db.execute("UPDATE sessions SET workspace_path = %s WHERE id = %s", (tree, session))
    LocalAgentRunManager(db).create(
        parent_session_id=manager,
        provider="claude",
        prompt="Run the developer seat.",
        agent_name="developer",
        child_session_id=session,
    )
    return session


async def admitted_team(world: TeamWorld, title: str) -> Team:
    """A devteam whose runbook admitted its tree and launched all four seats."""
    task = world.case.task(title)
    return await seated_team(world, task, world.child(task))


async def seated_team(world: TeamWorld, task: Task, runbook: Session) -> Team:
    """Admit ``runbook``'s team for ``task``, then launch all four seats in its tree."""
    binding = await world.admit(runbook, task)
    world.finish(runbook)
    manager = seat(world, runbook, binding, "dev-manager", manages=task)
    seats = [manager, *(seat(world, runbook, binding, role) for role in ROLES)]
    return Team(task, runbook, binding, seats)


def closed_at(task: Task) -> datetime:
    assert task.closed_at is not None
    return task.closed_at


def teardown(world: TeamWorld, team: Team) -> dict[str, Any]:
    """The teardown outcome the pass recorded on the team's admission output."""
    record = world.admit_step_output(team.runbook).get(TEARDOWN_KEY)
    return record if isinstance(record, dict) else {}


def branch_exists(world: TeamWorld, branch: str) -> bool:
    return world.case.git("branch", "--list", branch) != ""


def session_status(db: HubDatabase, session_id: str) -> str:
    session = SessionManager(db).get(session_id)
    assert session is not None
    return session.status


def daemon(db: HubDatabase, *, initialized: bool = True) -> GobbyRunner:
    """The daemon pieces the teardown uses; ``initialized`` False is a start still in progress."""
    init: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    if initialized:
        init.set_result(None)
    return cast(
        "GobbyRunner",
        SimpleNamespace(
            database=db,
            agent_runner=SimpleNamespace(),
            agent_lifecycle_monitor=None,
            completion_registry=None,
            task_manager=LocalTaskManager(db),
            session_manager=SessionManager(db),
            worktree_delete_executor=None,
            _subsystem_init_task=init,
        ),
    )


async def teardown_pass(
    db: HubDatabase, runner: GobbyRunner, at: datetime, delete: TeamTreeDeleter | None = None
) -> int:
    return await reconcile_team_teardowns(
        db,
        terminate=team_run_terminator(runner),
        delete=delete or team_tree_deleter(db, None),
        now=at,
    )
