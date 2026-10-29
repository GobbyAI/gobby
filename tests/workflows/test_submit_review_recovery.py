"""Developer submit recovers from authoritative review evidence after a hold."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.agents.team_lifecycle import seat_team
from gobby.agents.team_review import seat_team_held
from gobby.hooks.events import HookEventType
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.step_records import StepRecordError, StepSubmission, record_task_step
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from tests.agents.test_team_epic_review import epic_team, new_task, world
from tests.fixtures.review_approvals import grant_review_writes, reject_review
from tests.fixtures.team_case import TeamWorld
from tests.workflows.test_resumed_claim_binding import _already_claimed
from tests.workflows.test_seat_step_loops import PEER_ID, SESSION_ID, SHA, SeatRun, _seat, _set

__all__ = ["world"]
pytestmark = pytest.mark.unit


async def _admits(run: SeatRun, tool: str, tool_input: dict[str, Any] | None = None) -> bool:
    """Exercise pre-tool binding reconciliation through the real hook boundary."""
    SessionVariableManager(run.db).merge_variables(
        SESSION_ID, {**run.session_vars, "is_spawned_agent": True}
    )
    handler = WorkflowHookHandler(
        rule_engine=run.engine,
        session_manager=run.sessions,
        task_manager=LocalTaskManager(run.db),
        session_task_manager=SessionTaskManager(run.db),
    )
    if tool_input is None:
        tool_input = (
            {"command": "git status --short"} if tool == "Bash" else {"file_path": "src/example.py"}
        )
    event = run._event(HookEventType.BEFORE_TOOL, {"tool_name": tool, "tool_input": tool_input})
    event.metadata["_platform_session_id"] = SESSION_ID
    event.cwd = str(Path(__file__).resolve().parents[2])
    event.project_id = run.project_id
    return (await handler._evaluate_rules(event)).decision != "block"


async def _submitted(
    db: HubDatabase, *, project_id: str | None = None, task: Task | None = None
) -> tuple[SeatRun, Task]:
    definition = _seat(
        {
            "submit": {
                "allowed_tools": ["Bash", "Read", "mcp__gobby__call_tool"],
                "on_mcp_success": [_set("gobby-agents", "send_message", "candidate_sent", True)],
            }
        }
    )
    run = (
        SeatRun(db, "developer", definition=definition, project_id=project_id)
        if project_id is not None
        else SeatRun(db, "developer", definition=definition)
    )
    task = (
        LocalTaskManager(db).claim_task(task.id, SESSION_ID)
        if task is not None
        else run.task(owner=SESSION_ID)
    )
    run.session_vars.update(active_task_id=task.id, claimed_tasks={task.id: f"#{task.seq_num}"})
    await run.load(*run.vars["required_skills"])
    await run.call("gobby-tasks", "claim_task", {"task_id": task.id}, _already_claimed(task))
    await run.call("gobby-tasks", "get_task", {"task_id": task.id}, {"id": task.id})
    assert run.step == "implement"
    db.execute("UPDATE tasks SET commits = %s WHERE id = %s", (json.dumps([SHA]), task.id))
    await run.call(
        "gobby-tasks",
        "link_commit",
        {"task_id": task.id, "commit_sha": SHA},
        {"success": True, "production_changes": True},
    )
    record_task_step(
        db,
        task_id=task.id,
        session_id=SESSION_ID,
        step="implementation",
        submission=StepSubmission(
            status="approved",
            epoch=task.review_epoch,
            round_number=1,
            feedback="Focused validation passed.",
            commit_sha=SHA,
        ),
    )
    await run.send(f"EVENT=CANDIDATE TASK=#{task.seq_num} RUN=run WT=tree COMMIT={SHA} NOTE=ready")
    assert run.step == "submit"
    assert run.vars["candidate_sent"] is True
    grant_review_writes(db, PEER_ID, run.project_id)
    return run, task


async def test_recorded_rejection_returns_submit_without_message_read(temp_db: HubDatabase) -> None:
    run, task = await _submitted(temp_db)
    reject_review(temp_db, task_id=task.id, reviewer_session_id=PEER_ID, commit_sha=SHA)

    assert await _admits(run, "Bash")
    assert run.step == "implement"
    assert run.vars["bounced"] is True
    assert run.vars["implementation_complete"] is False


@pytest.mark.parametrize("stale", ["epoch", "sha", "implementation", "submit", "owner"])
async def test_stale_rejection_does_not_reopen_submit(temp_db: HubDatabase, stale: str) -> None:
    run, task = await _submitted(temp_db)
    reject_review(temp_db, task_id=task.id, reviewer_session_id=PEER_ID, commit_sha=SHA)
    if stale == "epoch":
        temp_db.execute(
            "UPDATE tasks SET review_epoch = review_epoch + 1 WHERE id = %s", (task.id,)
        )
    elif stale == "sha":
        temp_db.execute(
            "UPDATE tasks SET commits = %s WHERE id = %s", (json.dumps([SHA, "b" * 40]), task.id)
        )
    elif stale == "implementation":
        temp_db.execute(
            "DELETE FROM task_comments WHERE task_id = %s AND body::jsonb->>'step' = 'implementation'",
            (task.id,),
        )
    elif stale == "submit":
        instance = run.instances.get_for_session(SESSION_ID)
        assert instance is not None
        instance.step_entered_at = datetime.now(UTC) + timedelta(seconds=1)
        run.instances.save(instance)
    else:
        LocalTaskManager(temp_db).claim_task(task.id, PEER_ID, force=True)

    assert not await _admits(run, "Edit")
    assert run.step == "submit"
    assert run.vars["bounced"] is False


async def test_epic_hold_clears_before_rejection_recovers_developer(world: TeamWorld) -> None:
    db = world.case.db
    epic = new_task(world, "Recovery epic", epic=True)
    leaf = new_task(world, "Recovery leaf", parent=epic)
    team = await epic_team(world, epic)
    run, task = await _submitted(db, project_id=world.case.project_id, task=leaf)
    for session_id, role in ((SESSION_ID, "developer"), (PEER_ID, "code-reviewer")):
        runs = LocalAgentRunManager(db)
        agent_run = runs.create(
            parent_session_id=team.runbook.id,
            provider="claude",
            prompt="Run the team seat.",
            agent_name=role,
            child_session_id=session_id,
        )
        runs.start(agent_run.id)
        SessionManager(db).update_terminal_pickup_metadata(session_id, agent_run_id=agent_run.id)
    binding = seat_team(db, SESSION_ID)
    assert binding is not None
    tasks = LocalTaskManager(db)
    tasks.escalate_task(epic.id, reason="Explicit operator hold")
    assert seat_team_held(db, binding)
    with pytest.raises(StepRecordError, match="held"):
        reject_review(db, task_id=task.id, reviewer_session_id=PEER_ID, commit_sha=SHA)
    assert not await _admits(run, "Edit")
    assert run.step == "submit"

    tasks.de_escalate_task(epic.id, reason="Operator released hold")
    assert not seat_team_held(db, binding)
    reject_review(db, task_id=task.id, reviewer_session_id=PEER_ID, commit_sha=SHA)
    # A later hold must also suppress recovery of already-stored evidence.
    tasks.escalate_task(epic.id, reason="Second operator hold")
    assert not await _admits(run, "Edit")
    assert run.step == "submit"
    tasks.de_escalate_task(epic.id, reason="Operator released second hold")

    assert await _admits(run, "Bash")
    assert run.step == "implement"
    assert run.vars["bounced"] is True
