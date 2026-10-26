"""The review loop passes one task claim between the seats of one team (plan 3.2)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.agents.team_lifecycle import TeamAdmissionRefused, TeamBinding
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.spawn_agent import _factory
from gobby.mcp_proxy.tools.tasks import _claim_release, create_task_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._de_escalation import REVIEW_REJECTION_LIMIT
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.tasks.step_records import (
    StepRecordError,
    StepStatus,
    StepSubmission,
    TaskStep,
    TaskStepRecord,
    record_task_step,
)
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.agent_models import (
    AgentDefinitionBody,
    AgentPromptBlocks,
    AgentSelector,
    AgentStepWorkflowBody,
    AgentWorkflows,
)
from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_executor import step_invocation_id
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstance, AgentStepInstanceManager
from gobby.workflows.task_dirty_state import task_dirty_paths
from gobby.workflows.templates import TemplateEngine
from tests.fixtures.landing_case import bind_task_manager, make_landing_case
from tests.fixtures.review_approvals import grant_review_writes, grant_step_writes
from tests.fixtures.team_case import TITLE, TeamWorld, execution_id, git_in, make_team_world

pytestmark = pytest.mark.unit

DEVTEAM = PipelineDefinition.model_validate(
    yaml.safe_load(
        (
            Path(__file__).resolve().parents[2]
            / "src/gobby/install/shared/workflows/pipelines/devteam.yaml"
        ).read_text()
    )
)
SOL = {"provider": "codex", "model": "gpt-6.1-sol", "reasoning_effort": "medium"}
OPUS = {"provider": "claude", "model": "claude-opus-5-5", "reasoning_effort": "xhigh"}
# What the Orchestrator passes from the project policy's launch_defaults.devteam (Decision 5).
LAUNCH_DEFAULTS = {
    **{f"reviewer_{key}": value for key, value in SOL.items()},
    **{f"fresh_reviewer_{key}": value for key, value in OPUS.items()},
}
WORKSPACE = "pilot-workspace-id"
RENDERER = StepRenderer(TemplateEngine())


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TeamWorld:
    world = make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )

    def seat_body(name: str, *args: Any, **kwargs: Any) -> AgentDefinitionBody:
        """Every seat a devteam step names resolves to an installed definition."""
        return AgentDefinitionBody(
            name=name,
            prompts=AgentPromptBlocks(agent="Runbook seat"),
            workflows=AgentWorkflows(rule_selectors=AgentSelector()),
        )

    monkeypatch.setattr(_factory, "_load_agent_body", seat_body)
    return world


def seat(world: TeamWorld, runbook: Session, role: str, *, manages: Task | None = None) -> str:
    """A seat the team runbook launched; ``manages`` binds the Dev Manager's run to its task."""
    session = world.case.session(role, parent=runbook.id)
    if manages is not None:
        bind_task_manager(
            world.case.db,
            manager=session,
            parent=runbook.id,
            project_id=world.case.project_id,
            task=manages,
        )
    else:
        LocalAgentRunManager(world.case.db).create(
            parent_session_id=runbook.id,
            provider="claude",
            prompt=f"Run the {role} seat.",
            agent_name=role,
            child_session_id=session,
        )
    return session


async def hand_off(
    tools: InternalToolRegistry, caller: str, task: Task, holder: str, recipient: str
) -> dict[str, Any]:
    arguments = {
        "task_id": task.id,
        "holder_session_ref": holder,
        "recipient_session_ref": recipient,
        "reason": "Tests are committed; implementation starts",
    }
    with session_context_for_test(caller):
        return cast(dict[str, Any], await tools.call("hand_off_task_claim", arguments))


async def release(
    tools: InternalToolRegistry, caller: str, task: Task, holder: str
) -> dict[str, Any]:
    arguments = {"task_id": task.id, "holder_session_ref": holder, "reason": "Seat is stuck"}
    with session_context_for_test(caller):
        return cast(dict[str, Any], await tools.call("release_task_claim", arguments))


def claimed_tasks(db: HubDatabase, session_id: str) -> dict[str, Any]:
    claimed: dict[str, Any] = (
        SessionVariableManager(db).get_variables(session_id).get("claimed_tasks") or {}
    )
    return claimed


@dataclass(frozen=True)
class ReviewTeam:
    task: Task
    runbook: Session
    binding: TeamBinding
    manager: str
    developer: str
    sol: str
    candidate: str


def commit(db: HubDatabase, binding: TeamBinding, name: str) -> str:
    """Commit ``name`` in the team tree and link it as the task's latest commit."""
    tree = Path(binding.worktree_path)
    (tree / name).write_text(f"# {name}\n")
    git_in(tree, "add", name)
    git_in(tree, "commit", "-m", f"Add {name}")
    sha = git_in(tree, "rev-parse", "HEAD")
    commits = LocalTaskManager(db).get_task(binding.task_id).commits or []
    db.execute(
        "UPDATE tasks SET commits = %s WHERE id = %s",
        (json.dumps([*commits, sha]), binding.task_id),
    )
    return sha


def submit(
    db: HubDatabase,
    session_id: str,
    task_id: str,
    step: TaskStep,
    status: StepStatus,
    round_number: int,
    sha: str,
) -> tuple[TaskStepRecord, bool]:
    """Write a seat's step record for the task's current epoch through the real writer."""
    return record_task_step(
        db,
        task_id=task_id,
        session_id=session_id,
        step=step,
        submission=StepSubmission(
            status=status,
            epoch=LocalTaskManager(db).get_task(task_id).review_epoch,
            round_number=round_number,
            feedback=f"{step} {status} in round {round_number}",
            commit_sha=sha,
        ),
    )


async def team_in_review(world: TeamWorld, title: str = TITLE) -> ReviewTeam:
    """A devteam whose developer holds the claim and recorded the first candidate."""
    db, project_id = world.case.db, world.case.project_id
    task = world.case.task(title)
    runbook = world.child(task, DEVTEAM)
    binding = await world.admit(runbook, task)
    manager = seat(world, runbook, "dev-manager", manages=task)
    developer, sol = (seat(world, runbook, role) for role in ("developer", "code-reviewer"))
    grant_step_writes(db, developer, project_id, "fixture-developer", ("implementation",))
    grant_review_writes(db, sol, project_id)
    LocalTaskManager(db).claim_task(task.id, developer, force=True)
    candidate = commit(db, binding, "scroll.py")
    submit(db, developer, task.id, "implementation", "approved", 1, candidate)
    return ReviewTeam(task, runbook, binding, manager, developer, sol, candidate)


def session_ref(db: HubDatabase, session_id: str) -> str:
    session = SessionManager(db).get(session_id)
    assert session is not None
    return f"#{session.seq_num}"


async def refusal(admission: Awaitable[TeamBinding]) -> str:
    with pytest.raises(TeamAdmissionRefused) as refused:
        await admission
    return refused.value.code


def devteam_context(inputs: dict[str, Any]) -> dict[str, Any]:
    """The render context a devteam execution builds for ``inputs`` before its admit step."""
    values = {name: spec.get("default") for name, spec in DEVTEAM.inputs.items()}
    values.update(LAUNCH_DEFAULTS, workspace="pilot-workspace", report_to="gobby#14972")
    values.update(inputs)
    # With no live seat, the guard launches every requested seat, as check_runbook_seats does.
    requested = "code-reviewer" if values["fresh_reviewer"] else values["seats"]
    guard: dict[str, Any] = {
        "workspace_id": WORKSPACE,
        "launch": requested.split(","),
        "live": {},
        # A replacement names a team whose tab an earlier launch opened; a new team has none.
        "tab_exists": values["team_id"] is not None,
    }
    return {
        "inputs": values,
        "steps": {"guard": {"output": guard}},
        "_string_inputs": frozenset(
            name for name, spec in DEVTEAM.inputs.items() if spec.get("type") == "string"
        ),
    }


def render(run: Session, step_id: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """A devteam step's MCP arguments as ``run``'s executor renders them; None when skipped."""
    step = next((step for step in DEVTEAM.steps if step.id == step_id), None)
    assert step is not None, f"devteam has no {step_id} step"
    step_context = {**context, "invocation_id": step_invocation_id(execution_id(run), step_id)}
    if not RENDERER.should_run_step(step, step_context):
        return None
    assert step.mcp is not None and step.mcp.arguments is not None
    return RENDERER.render_mcp_arguments(step.mcp.arguments, step_context, drop_none=True)


async def admitted(world: TeamWorld, run: Session, inputs: dict[str, Any]) -> dict[str, Any]:
    """Run ``run``'s rendered admit step through admit_team; the context then holds its output."""
    context = devteam_context(inputs)
    arguments = render(run, "admit", context)
    assert arguments is not None
    with session_context_for_test(run.id):
        reply = cast(dict[str, Any], await world.tools.call("admit_team", arguments))
    assert reply.get("success") is True, reply
    context["steps"]["admit"] = {"output": reply}
    return context


def launch(run: Session, context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The MCP arguments of every devteam step that runs for ``context``, by step id."""
    rendered = {step.id: render(run, step.id, context) for step in DEVTEAM.steps}
    return {step_id: arguments for step_id, arguments in rendered.items() if arguments is not None}


def identity(arguments: dict[str, Any]) -> dict[str, Any]:
    return {key: arguments.get(key) for key in SOL}


async def spawn_seat(world: TeamWorld, run: Session, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call spawn_agent as ``run``'s rendered seat step does."""
    with session_context_for_test(run.id):
        return cast(
            dict[str, Any],
            await world.spawns.call("spawn_agent", {**arguments, "parent_session_id": run.id}),
        )


@pytest.mark.asyncio
async def test_claim_handoff_is_scoped_and_atomic(world: TeamWorld) -> None:
    db = world.case.db
    tasks = LocalTaskManager(db)
    tools = create_task_registry(tasks)
    task = world.case.task(TITLE)
    runbook = world.child(task)
    team = await world.admit(runbook, task)
    manager = seat(world, runbook, "dev-manager", manages=task)
    writer, developer, reviewer = (
        seat(world, runbook, role) for role in ("test-writer", "developer", "code-reviewer")
    )
    other_task = world.case.task("Another team's task")
    other_runbook = world.child(other_task)
    await world.admit(other_runbook, other_task)
    other_manager = seat(world, other_runbook, "dev-manager", manages=other_task)
    outsider = seat(world, other_runbook, "developer")
    tasks.claim_task(task.id, writer, force=True)

    def holder() -> str | None:
        return tasks.get_task(task.id).claimed_by_session_id

    # Only this team's Dev Manager moves the claim, and only between this team's seats.
    refused = await hand_off(tools, other_manager, task, writer, developer)
    assert refused.get("error_code") == "team_manager_required", refused
    refused = await hand_off(tools, developer, task, writer, developer)
    assert refused.get("error_code") == "team_manager_required", refused
    refused = await hand_off(tools, manager, task, writer, outsider)
    assert refused.get("error_code") == "team_seat_required", refused
    # A stale expected owner never moves the claim.
    refused = await hand_off(tools, manager, task, developer, reviewer)
    assert refused.get("error_code") == "task_claim_conflict", refused
    assert holder() == writer

    # Uncommitted task-attributed work keeps the claim with its holder.
    tree = Path(team.worktree_path)
    (tree / "test_scroll.py").write_text("def test_scroll() -> None: ...\n")
    SessionVariableManager(db).merge_variables(
        writer,
        {
            "task_edited_files": {task.id: ["test_scroll.py"]},
            "task_edited_file_checkouts": {task.id: {str(tree): ["test_scroll.py"]}},
        },
    )
    refused = await hand_off(tools, manager, task, writer, developer)
    assert refused.get("error_code") == "task_has_uncommitted_changes", refused
    assert holder() == writer
    git_in(tree, "add", "test_scroll.py")
    git_in(tree, "commit", "-m", "tests first")

    # Two concurrent hand-offs from the same holder: exactly one moves the claim.
    results = await asyncio.gather(
        hand_off(tools, manager, task, writer, developer),
        hand_off(tools, manager, task, writer, reviewer),
    )
    winners = [result for result in results if result.get("success") is True]
    assert len(winners) == 1, results
    (loser,) = (result for result in results if result.get("success") is not True)
    assert loser.get("error_code") == "task_claim_conflict", results
    recipient = winners[0]["handed_to_session_id"]
    assert recipient in (developer, reviewer)
    assert holder() == recipient
    assert task.id in claimed_tasks(db, recipient)
    assert task.id not in claimed_tasks(db, writer)
    event = TaskLifecycleEventManager(db).list_lifecycle_events(task.id)[-1]
    assert event.by_actor == manager
    assert writer in event.reason and recipient in event.reason

    # The same hand-off replayed is stale and changes nothing.
    replay = await hand_off(tools, manager, task, writer, recipient)
    assert replay.get("error_code") == "task_claim_conflict", replay
    assert holder() == recipient


@pytest.mark.asyncio
async def test_hand_off_keeps_the_closer_and_the_reviewer_eligible(world: TeamWorld) -> None:
    db = world.case.db
    tasks = LocalTaskManager(db)
    tools = create_task_registry(tasks)
    team = await team_in_review(world)
    task_id = team.task.id
    closer = seat(world, team.runbook, "closer")
    grant_step_writes(db, closer, world.case.project_id, "fixture-closer", ("closing",))
    # A former claimant never lands, so the seats that close the task never take its claim.
    refused = await hand_off(tools, team.manager, team.task, team.developer, team.manager)
    assert refused.get("error_code") == "closer_claim_forbidden", refused
    refused = await hand_off(tools, team.manager, team.task, team.developer, closer)
    assert refused.get("error_code") == "closer_claim_forbidden", refused
    # A former claimant never reviews, so the epoch's reviewer takes the claim only at the
    # second rejection and stays free to review the fix after the first.
    submit(db, team.sol, task_id, "review", "rejected", 1, team.candidate)
    refused = await hand_off(tools, team.manager, team.task, team.developer, team.sol)
    assert refused.get("error_code") == "reviewer_claim_early", refused
    fix = commit(db, team.binding, "scroll_fix.py")
    submit(db, team.developer, task_id, "implementation", "approved", 2, fix)
    submit(db, team.sol, task_id, "review", "rejected", 2, fix)
    moved = await hand_off(tools, team.manager, team.task, team.developer, team.sol)
    assert moved.get("success") is True, moved
    assert tasks.get_task(task_id).claimed_by_session_id == team.sol


@dataclass(frozen=True)
class SpikeTeam:
    task: Task
    manager: str
    developer: str
    writer: str
    researcher: str


async def spike_team(world: TeamWorld) -> SpikeTeam:
    """A research spike team whose developer holds the claim, with a bound researcher."""
    db, project_id = world.case.db, world.case.project_id
    task = world.case.task("Research the debt")
    db.execute(
        "UPDATE tasks SET task_type = 'research_spike', category = 'research' WHERE id = %s",
        (task.id,),
    )
    runbook = world.child(task)
    await world.admit(runbook, task)
    manager = seat(world, runbook, "dev-manager", manages=task)
    developer, writer = (seat(world, runbook, role) for role in ("developer", "test-writer"))
    researcher = world.case.session("researcher", parent=runbook.id)
    grant_step_writes(db, researcher, project_id, "researcher", ("implementation", "closing"))
    LocalAgentRunManager(db).create(
        parent_session_id=runbook.id,
        provider="claude",
        prompt="Research the spike.",
        agent_name="researcher",
        child_session_id=researcher,
        task_id=task.id,
    )
    tasks = LocalTaskManager(db)
    tasks.claim_task(task.id, developer, force=True)
    return SpikeTeam(tasks.get_task(task.id), manager, developer, writer, researcher)


@pytest.mark.asyncio
async def test_bound_researcher_is_not_the_dev_manager(world: TeamWorld) -> None:
    tasks = LocalTaskManager(world.case.db)
    team = await spike_team(world)

    refused = await hand_off(
        create_task_registry(tasks), team.researcher, team.task, team.developer, team.writer
    )

    assert refused.get("error_code") == "team_manager_required", refused
    assert tasks.get_task(team.task.id).claimed_by_session_id == team.developer


@pytest.mark.asyncio
async def test_researcher_closing_grant_still_receives_its_spike_claim(world: TeamWorld) -> None:
    tasks = LocalTaskManager(world.case.db)
    team = await spike_team(world)

    moved = await hand_off(
        create_task_registry(tasks), team.manager, team.task, team.developer, team.researcher
    )

    assert moved.get("success") is True, moved
    assert tasks.get_task(team.task.id).claimed_by_session_id == team.researcher


@pytest.mark.asyncio
async def test_hand_off_dirty_check_runs_off_the_event_loop(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registry runs hand_off_task_claim on a worker thread, and that worker runs the dirty check.
    tasks = LocalTaskManager(world.case.db)
    tools = create_task_registry(tasks)
    task = world.case.task(TITLE)
    runbook = world.child(task)
    team = await world.admit(runbook, task)
    manager = seat(world, runbook, "dev-manager", manages=task)
    writer, developer = (seat(world, runbook, role) for role in ("test-writer", "developer"))
    tasks.claim_task(task.id, writer, force=True)
    tree = Path(team.worktree_path)
    (tree / "test_scroll.py").write_text("def test_scroll() -> None: ...\n")
    SessionVariableManager(world.case.db).merge_variables(
        writer,
        {
            "task_edited_files": {task.id: ["test_scroll.py"]},
            "task_edited_file_checkouts": {task.id: {str(tree): ["test_scroll.py"]}},
        },
    )
    loop_running: list[bool] = []

    def recording(paths: set[str], root: str) -> set[str] | None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop_running.append(False)
        else:
            loop_running.append(True)
        return task_dirty_paths(paths, root)

    monkeypatch.setattr(_claim_release, "task_dirty_paths", recording)
    refused = await hand_off(tools, manager, task, writer, developer)
    assert refused.get("error_code") == "task_has_uncommitted_changes", refused
    assert loop_running == [False]


@pytest.mark.asyncio
async def test_only_the_holders_dev_manager_releases_its_claim(world: TeamWorld) -> None:
    tasks = LocalTaskManager(world.case.db)
    tools = create_task_registry(tasks)
    task = world.case.task(TITLE)
    runbook = world.child(task)
    await world.admit(runbook, task)
    manager = seat(world, runbook, "dev-manager", manages=task)
    unbound, developer = (seat(world, runbook, role) for role in ("dev-manager", "developer"))
    other_task = world.case.task("Another team's task")
    other_runbook = world.child(other_task)
    await world.admit(other_runbook, other_task)
    other_manager = seat(world, other_runbook, "dev-manager", manages=other_task)
    outsider = seat(world, other_runbook, "developer")

    # Another team's Dev Manager, an unbound manager seat and a team developer are refused.
    tasks.claim_task(task.id, developer, force=True)
    for caller in (other_manager, unbound, developer):
        refused = await release(tools, caller, task, developer)
        assert refused.get("error_code") == "coordinator_required", refused
    # The Dev Manager cannot release a claim held outside its team.
    tasks.claim_task(task.id, outsider, force=True)
    refused = await release(tools, manager, task, outsider)
    assert refused.get("error_code") == "coordinator_required", refused
    assert tasks.get_task(task.id).claimed_by_session_id == outsider

    tasks.claim_task(task.id, developer, force=True)
    released = await release(tools, manager, task, developer)
    assert released.get("success") is True, released
    assert released["released_by_session_id"] == manager
    assert tasks.get_task(task.id).claimed_by_session_id is None


@pytest.mark.asyncio
async def test_review_escalation_sequence(world: TeamWorld) -> None:
    db = world.case.db
    tasks = LocalTaskManager(db)
    task_tools = create_task_registry(tasks)
    # A reviewer holding the claim before the second rejection gains no implementation step.
    early = await team_in_review(world, "Hand the claim over too early")
    submit(db, early.sol, early.task.id, "review", "rejected", 1, early.candidate)
    tasks.claim_task(early.task.id, early.sol, force=True)
    with pytest.raises(StepRecordError, match="cannot write the implementation step"):
        submit(db, early.sol, early.task.id, "implementation", "approved", 2, early.candidate)
    # A fresh reviewer joins a named team, and only at the second rejection.
    early_run = world.child(early.task, DEVTEAM)
    unnamed = world.admit(early_run, early.task, fresh_reviewer=True, exclude_reviewers=[early.sol])
    assert await refusal(unnamed) == "fresh_reviewer_team_required"
    premature = world.admit(
        early_run,
        early.task,
        execution_id(early.runbook),
        fresh_reviewer=True,
        exclude_reviewers=[early.sol],
    )
    assert await refusal(premature) == "fresh_reviewer_round"

    team = await team_in_review(world)
    task_id, team_id = team.task.id, execution_id(team.runbook)
    # Rejection one returns the work to the original developer; the Sol reviewer cannot fix.
    submit(db, team.sol, task_id, "review", "rejected", 1, team.candidate)
    with pytest.raises(StepRecordError, match="cannot write the implementation step"):
        submit(db, team.sol, task_id, "implementation", "approved", 2, team.candidate)
    fix = commit(db, team.binding, "scroll_fix.py")
    submit(db, team.developer, task_id, "implementation", "approved", 2, fix)
    submit(db, team.sol, task_id, "review", "rejected", 2, fix)
    # Rejection two alone grants nothing: the Sol reviewer fixes only once it holds the claim.
    with pytest.raises(StepRecordError, match="cannot write the implementation step"):
        submit(db, team.sol, task_id, "implementation", "approved", 3, fix)

    # The fresh reviewer is admitted only once the claim reached the Sol reviewer and the
    # request excludes every reviewer of the epoch.
    fresh_run = world.child(team.task, DEVTEAM)
    early_request = world.admit(
        fresh_run, team.task, team_id, fresh_reviewer=True, exclude_reviewers=[team.sol]
    )
    assert await refusal(early_request) == "fresh_reviewer_claim"
    moved = await hand_off(task_tools, team.manager, team.task, team.developer, team.sol)
    assert moved.get("success") is True, moved
    partial_request = world.admit(fresh_run, team.task, team_id, fresh_reviewer=True)
    assert await refusal(partial_request) == "fresh_reviewer_exclusions"

    # Rejection two made the Sol reviewer the fixer, which never reviews its own fix.
    second_fix = commit(db, team.binding, "scroll_fix_2.py")
    submit(db, team.sol, task_id, "implementation", "approved", 3, second_fix)
    with pytest.raises(StepRecordError, match="claimant"):
        submit(db, team.sol, task_id, "review", "rejected", 3, second_fix)

    # The team launched its seats with the Sol reviewer and opened the branch's tab.
    initial = launch(team.runbook, await admitted(world, team.runbook, {"task": task_id}))
    seats = ["dev-manager", "test-writer", "developer", "code-reviewer"]
    assert list(initial) == ["guard", "admit", *seats]
    assert identity(initial["code-reviewer"]) == SOL
    assert initial["dev-manager"]["placement"] == {
        "tab": {"workspace": WORKSPACE, "title": f"Dev Team - {task_id}"}
    }
    # The Orchestrator's relaunch for REJECTION_2 starts only an Opus reviewer in that tab.
    fresh_inputs = {
        "task": task_id,
        "team_id": team_id,
        "fresh_reviewer": True,
        "exclude_reviewers": session_ref(db, team.sol),
    }
    fresh = launch(fresh_run, await admitted(world, fresh_run, fresh_inputs))
    assert list(fresh) == ["guard", "admit", "code-reviewer"]
    assert fresh["guard"]["requested"] == "code-reviewer"
    reviewer = fresh["code-reviewer"]
    assert identity(reviewer) == OPUS
    assert reviewer["project_path"] == team.binding.worktree_path
    assert "task_id" not in reviewer
    assert reviewer["placement"] == {
        "split": {
            "workspace": WORKSPACE,
            "tab_title": f"Dev Team - {task_id}",
            "axis": "right",
            "title": "Code Reviewer",
        }
    }
    spawned = await spawn_seat(world, fresh_run, reviewer)
    assert spawned.get("success") is True, spawned
    (launched,) = world.launches
    assert identity(launched) == OPUS
    assert launched["reserved_run_id"] == step_invocation_id(
        execution_id(fresh_run), "code-reviewer"
    )

    # Rejection three by the fresh reviewer escalates the task and releases the claim.
    fresh_seat = seat(world, fresh_run, "code-reviewer")
    grant_review_writes(db, fresh_seat, world.case.project_id)
    submit(db, fresh_seat, task_id, "review", "rejected", 3, second_fix)
    escalated = tasks.get_task(task_id)
    assert escalated.escalated_at is not None
    assert escalated.claimed_by_session_id is None


@pytest.mark.asyncio
async def test_review_loop_recovers_idempotently(world: TeamWorld) -> None:
    db = world.case.db
    tasks = LocalTaskManager(db)
    team = await team_in_review(world)
    task_id, team_id = team.task.id, execution_id(team.runbook)
    submit(db, team.sol, task_id, "review", "rejected", 1, team.candidate)
    rejection, created = submit(db, team.sol, task_id, "review", "rejected", 2, team.candidate)
    assert created
    moved = await hand_off(
        create_task_registry(tasks), team.manager, team.task, team.developer, team.sol
    )
    assert moved.get("success") is True, moved

    # A duplicated verdict delivery replays the stored rejection and moves nothing.
    replay, created = submit(db, team.sol, task_id, "review", "rejected", 2, team.candidate)
    assert (replay.id, created) == (rejection.id, False)
    task = tasks.get_task(task_id)
    assert (task.review_fail_count, task.claimed_by_session_id) == (2, team.sol)
    assert task.escalated_at is None

    # Two relaunches race for the epoch's one fresh reviewer: the team admits exactly one.
    runs = [world.child(team.task, DEVTEAM) for _ in range(2)]
    results = await asyncio.gather(
        *(
            world.admit(run, team.task, team_id, fresh_reviewer=True, exclude_reviewers=[team.sol])
            for run in runs
        ),
        return_exceptions=True,
    )
    assert [type(result) for result in results].count(TeamBinding) == 1, results
    (winner,) = (run for run, result in zip(runs, results, strict=True) if result == team.binding)
    codes = [result.code for result in results if isinstance(result, TeamAdmissionRefused)]
    assert codes == ["fresh_reviewer_exists"], results
    assert world.admit_step_output(team.runbook) == {
        "team": asdict(team.binding),
        "fresh_reviewers": {str(task.review_epoch): execution_id(winner)},
    }

    # A resumed winner re-runs its admit step, keeps the team and adopts its launched seat.
    fresh_inputs = {
        "task": task_id,
        "team_id": team_id,
        "fresh_reviewer": True,
        "exclude_reviewers": session_ref(db, team.sol),
    }
    context = await admitted(world, winner, fresh_inputs)
    assert context["steps"]["admit"]["output"]["team"] == asdict(team.binding)
    reviewer = launch(winner, context)["code-reviewer"]
    first = await spawn_seat(world, winner, reviewer)
    assert first.get("success") is True, first
    runs_store = LocalAgentRunManager(db)
    runs_store.create(
        parent_session_id=winner.id,
        provider=OPUS["provider"],
        prompt="Review the fixed candidate.",
        agent_name="code-reviewer",
        run_id=reviewer["reserved_run_id"],
    )
    runs_store.start(reviewer["reserved_run_id"])
    again = await spawn_seat(world, winner, reviewer)
    assert (again.get("adopted"), again.get("run_id")) == (True, reviewer["reserved_run_id"])
    assert len(world.launches) == 1
    assert tasks.get_task(task_id).claimed_by_session_id == team.sol


# Relayed CANDIDATE intake (#24974): the task's bound Dev Manager may relay the
# claimant's CANDIDATE to its team reviewer; it binds the claimant's linked SHA.
NOT_LINKED = "CANDIDATE not bound: COMMIT is not linked to the claimant's task"
NOT_CLAIMANT = "CANDIDATE not bound: sender does not claim task"


def reviewer_at_await(db: HubDatabase, session_id: str) -> None:
    """Run ``session_id`` as a code-reviewer step instance waiting for a candidate."""
    agent = yaml.safe_load((get_bundled_agents_path() / "code-reviewer.yaml").read_text())
    workflow = agent["step_workflow"]
    SessionVariableManager(db).set_variable(session_id, "_agent_type", "code-reviewer")
    AgentStepInstanceManager(db).save(
        AgentStepInstance(
            id=str(uuid.uuid4()),
            session_id=session_id,
            agent_name="code-reviewer",
            snapshot=AgentStepWorkflowBody.model_validate(workflow),
            enabled=True,
            current_step="await",
            step_entered_at=datetime.now(UTC),
            variables=dict(workflow["variables"]),
        )
    )


async def read_candidate(
    db: HubDatabase, reviewer: str, sender: str, task: Task, sha: str
) -> tuple[dict[str, Any], AgentStepInstance]:
    """Deliver a CANDIDATE from ``sender`` and consume it the way the reviewer does."""
    messages = InterSessionMessageManager(db)
    content = f"EVENT=CANDIDATE TASK=#{task.seq_num} RUN=run WT=tree COMMIT={sha} NOTE=relayed"
    message = messages.create_message(sender, reviewer, content)
    registry = InternalToolRegistry(name="gobby-agents", description="Candidate intake")
    add_messaging_tools(registry, messages, SessionManager(db), db)
    arguments = {"message_id": message.id}
    with session_context_for_test(reviewer):
        result = cast(dict[str, Any], await registry.call("get_inter_session_message", arguments))
    assert result["success"] is True, result
    call = {
        "server_name": "gobby-agents",
        "tool_name": "get_inter_session_message",
        "arguments": arguments,
    }
    data = {"tool_name": "mcp__gobby__call_tool", "tool_input": call, "tool_output": result}
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=reviewer,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=normalize_tool_fields(data),
        metadata={},
    )
    engine = RuleEngine(db, session_manager=SessionManager(db), task_manager=LocalTaskManager(db))
    variables = SessionVariableManager(db).get_variables(reviewer)
    await engine.evaluate(event, session_id=reviewer, variables=variables)
    instance = AgentStepInstanceManager(db).get_for_session(reviewer)
    assert instance is not None
    return result, instance


@pytest.mark.parametrize("sender", ["claimant", "bound-manager"])
@pytest.mark.asyncio
async def test_bound_dev_manager_relayed_candidate_opens_review(
    world: TeamWorld, sender: str
) -> None:
    team = await team_in_review(world)
    reviewer_at_await(world.case.db, team.sol)
    session = team.developer if sender == "claimant" else team.manager

    result, instance = await read_candidate(
        world.case.db, team.sol, session, team.task, team.candidate
    )

    assert "candidate_binding_error" not in result, result
    assert instance.current_step == "review"
    assert instance.variables["candidate_received"] is True
    assert instance.variables["candidate_task"] == team.task.id
    assert instance.variables["candidate_sha"] == team.candidate


@pytest.mark.asyncio
async def test_relayed_candidate_for_unlinked_commit_is_refused(world: TeamWorld) -> None:
    team = await team_in_review(world)
    reviewer_at_await(world.case.db, team.sol)
    # A real commit in the team tree that the claimant never linked to the task.
    tree = Path(team.binding.worktree_path)
    (tree / "unlinked.py").write_text("# unlinked\n")
    git_in(tree, "add", "unlinked.py")
    git_in(tree, "commit", "-m", "Add unlinked")
    unlinked = git_in(tree, "rev-parse", "HEAD")
    assert unlinked not in (LocalTaskManager(world.case.db).get_task(team.task.id).commits or [])

    result, instance = await read_candidate(
        world.case.db, team.sol, team.manager, team.task, unlinked
    )

    assert result.get("candidate_binding_error") == NOT_LINKED
    assert instance.current_step == "await"
    assert instance.variables["candidate_received"] is False
    assert instance.variables["candidate_sha"] is None


@pytest.mark.parametrize("sender", ["unbound-manager", "other-team-manager", "team-developer"])
@pytest.mark.asyncio
async def test_candidate_from_non_claimant_non_manager_stays_refused(
    world: TeamWorld, sender: str
) -> None:
    team = await team_in_review(world)
    reviewer_at_await(world.case.db, team.sol)
    if sender == "unbound-manager":
        session = seat(world, team.runbook, "dev-manager")
    elif sender == "team-developer":
        session = seat(world, team.runbook, "developer")
    else:
        other_task = world.case.task("Another team's task")
        other_runbook = world.child(other_task, DEVTEAM)
        await world.admit(other_runbook, other_task)
        session = seat(world, other_runbook, "dev-manager", manages=other_task)

    result, instance = await read_candidate(
        world.case.db, team.sol, session, team.task, team.candidate
    )

    assert result.get("candidate_binding_error") == NOT_CLAIMANT
    assert instance.current_step == "await"
    assert instance.variables["candidate_received"] is False


async def _rejected_twice_and_handed_over(world: TeamWorld) -> ReviewTeam:
    """A team whose Sol reviewer holds the claim after the second rejection."""
    db = world.case.db
    team = await team_in_review(world)
    submit(db, team.sol, team.task.id, "review", "rejected", 1, team.candidate)
    submit(db, team.sol, team.task.id, "review", "rejected", 2, team.candidate)
    moved = await hand_off(
        create_task_registry(LocalTaskManager(db)),
        team.manager,
        team.task,
        team.developer,
        team.sol,
    )
    assert moved.get("success") is True, moved
    return team


@pytest.mark.asyncio
async def test_failed_fresh_reviewer_spawn_frees_the_epoch_slot(world: TeamWorld) -> None:
    db = world.case.db
    team = await _rejected_twice_and_handed_over(world)
    task_id, team_id = team.task.id, execution_id(team.runbook)
    epoch = LocalTaskManager(db).get_task(task_id).review_epoch
    fresh_inputs = {
        "task": task_id,
        "team_id": team_id,
        "fresh_reviewer": True,
        "exclude_reviewers": session_ref(db, team.sol),
    }

    # The first fresh-reviewer execution admits; its code-reviewer spawn fails, and its pipeline
    # fails with it.
    first_run = world.child(team.task, DEVTEAM)
    admitted_first = await world.admit(
        first_run, team.task, team_id, fresh_reviewer=True, exclude_reviewers=[team.sol]
    )
    assert admitted_first == team.binding
    reviewer = launch(first_run, await admitted(world, first_run, fresh_inputs))["code-reviewer"]
    world.spawn_replies.append(
        {
            "success": False,
            "error": "Workspace needs exactly one tab titled 'task-24824'; 0 tabs have that title.",
        }
    )
    failed = await spawn_seat(world, first_run, reviewer)
    assert failed.get("success") is False, failed
    world.fail(first_run)

    # The failed execution holds no live reviewer, so a relaunch for the same epoch admits and
    # spawns the one fresh reviewer.
    second_run = world.child(team.task, DEVTEAM)
    admitted_second = await world.admit(
        second_run, team.task, team_id, fresh_reviewer=True, exclude_reviewers=[team.sol]
    )
    assert admitted_second == team.binding
    assert world.admit_step_output(team.runbook)["fresh_reviewers"] == {
        str(epoch): execution_id(second_run)
    }
    relaunch = launch(second_run, await admitted(world, second_run, fresh_inputs))["code-reviewer"]
    spawned = await spawn_seat(world, second_run, relaunch)
    assert spawned.get("success") is True, spawned
    (launched,) = world.launches
    assert identity(launched) == OPUS
    assert launched["reserved_run_id"] == step_invocation_id(
        execution_id(second_run), "code-reviewer"
    )


@pytest.mark.asyncio
async def test_live_fresh_reviewer_still_blocks_a_second_one(world: TeamWorld) -> None:
    db = world.case.db
    team = await _rejected_twice_and_handed_over(world)
    task_id, team_id = team.task.id, execution_id(team.runbook)
    fresh_inputs = {
        "task": task_id,
        "team_id": team_id,
        "fresh_reviewer": True,
        "exclude_reviewers": session_ref(db, team.sol),
    }

    # The first fresh reviewer spawns and runs; its runbook then completes as a fire-and-forget
    # runbook does, so the live reviewer, not the execution status, holds the epoch's slot.
    first_run = world.child(team.task, DEVTEAM)
    await world.admit(
        first_run, team.task, team_id, fresh_reviewer=True, exclude_reviewers=[team.sol]
    )
    reviewer = launch(first_run, await admitted(world, first_run, fresh_inputs))["code-reviewer"]
    spawned = await spawn_seat(world, first_run, reviewer)
    assert spawned.get("success") is True, spawned
    runs = LocalAgentRunManager(db)
    runs.create(
        parent_session_id=first_run.id,
        provider=OPUS["provider"],
        prompt="Review the fixed candidate.",
        agent_name="code-reviewer",
        run_id=reviewer["reserved_run_id"],
    )
    runs.start(reviewer["reserved_run_id"])
    world.finish(first_run)

    second_run = world.child(team.task, DEVTEAM)
    refused = world.admit(
        second_run, team.task, team_id, fresh_reviewer=True, exclude_reviewers=[team.sol]
    )
    assert await refusal(refused) == "fresh_reviewer_exists"


@pytest.mark.asyncio
async def test_round_reset_leaves_no_claim_holder_for_hand_off(world: TeamWorld) -> None:
    """After escalation and an audited round reset, no seat holds the claim and hand-off agrees."""
    db = world.case.db
    tasks = LocalTaskManager(db)
    tools = create_task_registry(tasks)
    team = await team_in_review(world, "Reset an exhausted review round")
    task, manager, developer = team.task, team.manager, team.developer

    # The developer claims through the MCP path, which records its claimed task state.
    tasks.release_task_claim(task.id)
    with session_context_for_test(developer):
        claimed = await tools.call("claim_task", {"task_id": task.id})
    assert "error" not in claimed, claimed
    assert tasks.get_task(task.id).claimed_by_session_id == developer
    assert task.id in claimed_tasks(db, developer)

    # Escalation releases the claim and the developer's claimed task state.
    with session_context_for_test(developer):
        escalated = await tools.call("escalate_task", {"task_id": task.id, "reason": "Rejected"})
    assert escalated == {}, escalated
    assert tasks.get_task(task.id).claimed_by_session_id is None
    assert task.id not in claimed_tasks(db, developer)

    # The audited round reset clears the escalation and starts a new epoch. It must not
    # resurrect the released claim.
    tasks.update_task(task.id, review_fail_count=REVIEW_REJECTION_LIMIT)
    epoch = tasks.get_task(task.id).review_epoch
    reset = tasks.de_escalate_task(task.id, reason="Triage audited reset")
    assert reset.is_escalated is False
    assert reset.review_epoch == epoch + 1
    assert reset.claimed_by_session_id is None
    assert task.id not in claimed_tasks(db, developer)

    # Neither a former holder nor a seat that never held the claim is the holder now.
    refused = await hand_off(tools, manager, task, developer, developer)
    assert refused.get("error_code") == "task_claim_conflict", refused
    refused = await hand_off(tools, manager, task, manager, developer)
    assert refused.get("error_code") == "task_claim_conflict", refused
    assert tasks.get_task(task.id).claimed_by_session_id is None

    # A seat that really holds the claim still gets same_seat when it names itself.
    with session_context_for_test(developer):
        reclaimed = await tools.call("claim_task", {"task_id": task.id})
    assert "error" not in reclaimed, reclaimed
    held = await hand_off(tools, manager, task, developer, developer)
    assert held.get("error_code") == "same_seat", held
