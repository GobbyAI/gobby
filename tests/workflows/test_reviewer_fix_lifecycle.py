"""A reviewer can leave a finished fixer unit and review the next task (#25406)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from gobby.hooks.events import HookEventType
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._de_escalation import de_escalate_task
from gobby.storage.tasks._transitions import claim_task, release_task_claim
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import patch_local_machine_id
from tests.fixtures.review_approvals import approve_review
from tests.workflows.test_seat_step_loops import (
    AUTHORITY_MACHINE_ID,
    COORDINATOR_ID,
    OWNER_ID,
    PEER_ID,
    PROJECT_ID,
    SESSION_ID,
    SHA,
    SeatRun,
    _bundled_code_reviewer,
    _team_runbook,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def fixer(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SeatRun, Task]:
    patch_local_machine_id(monkeypatch, AUTHORITY_MACHINE_ID)
    run = SeatRun(temp_db, "code-reviewer", definition=_bundled_code_reviewer())
    LocalProjectCheckoutManager(temp_db).register(AUTHORITY_MACHINE_ID, PROJECT_ID, str(tmp_path))
    await run.load(*run.vars["required_skills"])
    task = run.task(owner=OWNER_ID)
    release_task_claim(temp_db, task.id)
    claim_task(temp_db, task.id, SESSION_ID)
    run.session_vars.update(SessionVariableManager(temp_db).get_variables(SESSION_ID))
    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": task.id},
        {"success": True, "task_id": task.id, "ref": f"#{task.seq_num}"},
    )
    assert (run.step, run.vars["fixing"]) == ("fix", True)
    return run, task


@pytest.mark.parametrize("completion", ["closed", "released", "epoch_reset"])
async def test_finished_fix_returns_to_await_and_admits_next_review(
    fixer: tuple[SeatRun, Task], completion: str
) -> None:
    run, task = fixer
    tasks = LocalTaskManager(run.db)
    if completion == "closed":
        tasks.close_task(task.id, reason="done", force=True)
    elif completion == "released":
        release_task_claim(run.db, task.id)
        claim_task(run.db, task.id, OWNER_ID)
    else:
        # Arrange an exhausted epoch; exercise its reset through the real transition.
        run.db.execute("UPDATE tasks SET review_fail_count = 3 WHERE id = %s", (task.id,))
        tasks.escalate_task(task.id, reason="Review epoch exhausted")
        reset = de_escalate_task(run.db, task.id, reason="Retry the review epoch")
        assert reset.review_epoch == task.review_epoch + 1

    run.session_vars.update(SessionVariableManager(run.db).get_variables(SESSION_ID))
    # A real BEFORE_TOOL reconciliation must repair the seat before enforcing its allow-list.
    assert await run.admits("gobby-agents", "wait_for_coordination")
    assert (run.step, run.vars["fixing"]) == ("await", False)

    next_task = run.task()
    await run.intake(next_task)
    assert run.step == "review"
    await run.call(
        "gobby-tasks",
        "link_task_to_session",
        {"task_id": next_task.id, "role": "reviewing"},
    )
    run.record_approval(next_task)
    await run.call(
        "gobby-tasks",
        "record_review_step",
        {"task_id": next_task.id, "status": "approved", "commit_sha": SHA},
    )
    assert run.vars["verdict_ready"] is True


async def test_live_fix_keeps_its_edit_authority(fixer: tuple[SeatRun, Task]) -> None:
    run, _task = fixer
    assert await run.admits_native("Edit", {"file_path": "src/gobby/example.py"})
    assert (run.step, run.vars["fixing"]) == ("fix", True)
    assert not await run.admits("gobby-tasks", "record_review_step")


async def test_fixer_candidate_hands_claim_to_fresh_reviewer(
    fixer: tuple[SeatRun, Task],
) -> None:
    run, task = fixer
    team = _team_runbook(run.db, task)
    run.db.execute(
        "UPDATE sessions SET parent_session_id = %s WHERE id IN (%s, %s, %s)",
        (team, SESSION_ID, PEER_ID, COORDINATOR_ID),
    )
    AgentDefinitionManager(run.db).create(
        "dev-manager",
        {
            "name": "dev-manager",
            "version": "1.0",
            "prompts": {"agent": "Manage this team."},
            "workflows": {
                "rule_selectors": {"include": []},
                "variables": {"team_manager": True},
            },
            "capabilities": ["manage_task_claims"],
            "task_step_writes": ["closing"],
        },
        project_id=PROJECT_ID,
    )
    SessionVariableManager(run.db).set_variable(COORDINATOR_ID, "_agent_type", "dev-manager")
    runs = LocalAgentRunManager(run.db)
    for session, role, bound in (
        (SESSION_ID, "code-reviewer", None),
        (PEER_ID, "code-reviewer", None),
        (COORDINATOR_ID, "dev-manager", task.id),
    ):
        record = runs.create(
            parent_session_id=team,
            provider="codex",
            prompt="Fixer review handoff",
            agent_name=role,
            child_session_id=session,
            task_id=bound,
        )
        runs.start(record.id)
        run.db.execute("UPDATE sessions SET agent_run_id = %s WHERE id = %s", (record.id, session))

    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    peer = instance.model_copy(deep=True)
    peer.id = str(uuid4())
    peer.session_id = PEER_ID
    peer.current_step = "await"
    peer.variables = dict(peer.workflow.variables)
    run.instances.save(peer)

    LocalTaskManager(run.db).link_commit(task.id, SHA)
    candidate = f"EVENT=CANDIDATE TASK={task.id} RUN=fresh-review WT=tree COMMIT={SHA} NOTE=fixed"
    await run.send(candidate, PEER_ID)
    assert LocalTaskManager(run.db).get_task(task.id).claimed_by_session_id == PEER_ID
    assert (run.step, run.vars["fixing"]) == ("await", False)

    # Continue the protocol as the fresh reviewer, using its own real step instance.
    message = InterSessionMessageManager(run.db).create_message(SESSION_ID, PEER_ID, candidate)
    call = {
        "server_name": "gobby-agents",
        "tool_name": "get_inter_session_message",
        "arguments": {"message_id": message.id},
    }
    incoming = replace(
        run._event(
            HookEventType.AFTER_TOOL,
            {
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": call,
                "tool_output": {"success": True, "message": message.to_dict()},
            },
        ),
        session_id=PEER_ID,
    )
    await run.engine.evaluate(incoming, session_id=PEER_ID, variables=run.session_vars)
    reviewing_peer = run.instances.get_for_session(PEER_ID)
    assert reviewing_peer is not None and reviewing_peer.current_step == "review"
    SessionTaskManager(run.db).link_task(PEER_ID, task.id, "reviewing")
    approve_review(run.db, task_id=task.id, reviewer_session_id=PEER_ID, commit_sha=SHA)
    verdict = InterSessionMessageManager(run.db).create_message(
        PEER_ID,
        COORDINATOR_ID,
        f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={SHA} VERDICT=LAND NOTE=approved",
    )
    outgoing = replace(
        run._event(
            HookEventType.AFTER_TOOL,
            {
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-agents",
                    "tool_name": "send_message",
                    "arguments": {
                        "target": "session",
                        "target_id": COORDINATOR_ID,
                        "content": verdict.content,
                    },
                },
                "tool_output": {"success": True, "message_ids": [verdict.id]},
            },
        ),
        session_id=PEER_ID,
    )
    await run.engine.evaluate(outgoing, session_id=PEER_ID, variables=run.session_vars)
    assert LocalTaskManager(run.db).get_task(task.id).claimed_by_session_id == COORDINATOR_ID
