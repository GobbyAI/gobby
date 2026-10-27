"""Folded DevTeam seat fixes from #24824: a reviewer holds its candidate on a PROOF_REQUEST,
a reviewer may wait on a background run with Monitor, and the bare CANDIDATE form is refused.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.seat_messages import _CANDIDATE_GRAMMAR
from tests.workflows.test_seat_step_loops import (
    OWNER_ID,
    REPLACEMENT_SHA,
    SESSION_ID,
    SHA,
    SeatRun,
)

pytestmark = pytest.mark.unit

_ADVANCE_INBOX = {
    "server": "gobby-agents",
    "tool": "get_inter_session_message",
    "action": "advance_seat",
}
_ADVANCE_SEND = {"server": "gobby-agents", "tool": "send_message", "action": "advance_seat"}


def _reviewer(*, await_tools: list[str] | str, review_tools: list[str] | str) -> dict[str, Any]:
    """A synthetic reviewer seat: a candidate waits in await, is judged in review and verdict.

    It carries only the steps and transitions these tests drive; the seat protocol itself
    is applied by the engine's advance_seat handler.
    """
    return {
        "name": "code-reviewer",
        "prompts": {"agent": "Synthetic reviewer seat."},
        "workflows": {"rule_selectors": {"include": [], "exclude": []}},
        "step_workflow": {
            "variables": {
                "candidate_received": False,
                "verdict_ready": False,
                "candidate_task": None,
                "candidate_sha": None,
                "candidate_owner": None,
                "candidate_received_at": None,
                "candidate_disposition": None,
                "required_skills": [],
            },
            "steps": [
                {
                    "name": "await",
                    "allowed_tools": await_tools,
                    "on_mcp_success": [_ADVANCE_INBOX, _ADVANCE_SEND],
                    "transitions": [{"to": "review", "when": "vars.candidate_received"}],
                },
                {
                    "name": "review",
                    "allowed_tools": review_tools,
                    "on_mcp_success": [_ADVANCE_INBOX, _ADVANCE_SEND],
                    "transitions": [
                        {"to": "await", "when": "not vars.candidate_received"},
                        {"to": "verdict", "when": "vars.verdict_ready"},
                    ],
                },
                {
                    "name": "verdict",
                    "allowed_tools": "all",
                    "on_mcp_success": [_ADVANCE_INBOX, _ADVANCE_SEND],
                    "transitions": [
                        {"to": "await", "when": "not vars.candidate_received"},
                        {
                            "to": "review",
                            "when": "vars.candidate_received and not vars.verdict_ready",
                        },
                    ],
                },
            ],
        },
    }


# A reviewer holding a candidate keeps its review until a replacement candidate arrives.


async def test_reviewer_proof_request_keeps_review_until_replacement(temp_db: HubDatabase) -> None:
    run = SeatRun(
        temp_db,
        "code-reviewer",
        definition=_reviewer(await_tools="all", review_tools="all"),
    )
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    assert run.step == "review"

    await run.send(
        f"EVENT=PROOF_REQUEST TASK={task.id} SHA={SHA} NOTE=merge-with-base-conflicts",
        OWNER_ID,
    )
    # No verdict was sent and no rejection recorded: the unit stays open in review.
    assert run.step == "review"
    assert run.vars["candidate_sha"] == SHA

    await run.intake(task, REPLACEMENT_SHA)
    assert run.step == "review"
    assert run.vars["candidate_sha"] == REPLACEMENT_SHA
    assert run.vars["verdict_ready"] is False


# A reviewer waiting on a background test run may wait in-turn with Monitor, only in review.


async def test_review_step_admits_monitor_for_background_runs(temp_db: HubDatabase) -> None:
    run = SeatRun(
        temp_db,
        "code-reviewer",
        definition=_reviewer(
            await_tools=["mcp__gobby__call_tool"],
            review_tools=["mcp__gobby__call_tool", "Monitor"],
        ),
    )
    monitor = {"command": "until ! kill -0 4242; do sleep 5; done", "description": "pytest"}
    # Idle await ends turns on durable waits, so it never admits an in-turn Monitor.
    assert not await run.admits_native("Monitor", monitor)

    await run.load(*run.vars["required_skills"])
    await run.intake(run.task())
    assert run.step == "review"
    assert await run.admits_native("Monitor", monitor)


# The bare CANDIDATE form: refused at send with the canonical grammar.


@pytest.fixture
def messaging(monkeypatch: pytest.MonkeyPatch) -> tuple[InternalToolRegistry, MagicMock]:
    sessions = MagicMock()
    sessions.get = MagicMock(
        side_effect=lambda ref: MagicMock(
            id=ref, project_id="p", parent_session_id=None, agent_depth=0, status="active"
        )
    )
    sessions.resolve_session_reference = MagicMock(side_effect=lambda ref, project_id=None: ref)
    sessions.is_ancestor = MagicMock(return_value=False)
    messages = MagicMock()
    monkeypatch.setattr(
        "gobby.workflows.engine.seat_messages.SessionManager", MagicMock(return_value=sessions)
    )
    monkeypatch.setattr("gobby.workflows.engine.seat_messages.LocalTaskManager", MagicMock())
    registry = InternalToolRegistry(name="gobby-agents", description="Candidate send")
    add_messaging_tools(
        registry=registry, message_manager=messages, session_manager=sessions, db=MagicMock()
    )
    return registry, messages


@pytest.mark.parametrize(
    "line",
    [
        f"CANDIDATE TASK_TITLE=Scroll RUN=red,green WT=tree COMMIT={SHA}",
        f"CANDIDATE TASK=#7 TASK_TITLE=Scroll RUN=run WT=tree COMMIT={SHA} NOTE=ready",
    ],
)
async def test_bare_candidate_is_refused_with_canonical_grammar(
    messaging: tuple[InternalToolRegistry, MagicMock], line: str
) -> None:
    registry, messages = messaging
    result = await registry.call(
        "send_message",
        {"from_session": "s-from", "target": "session", "target_id": "s-to", "content": line},
    )
    assert result["success"] is False, result
    assert result["candidate_binding_error"] == _CANDIDATE_GRAMMAR
    assert "EVENT=CANDIDATE TASK=" in _CANDIDATE_GRAMMAR
    messages.create_message.assert_not_called()


async def test_held_claim_result_binds_the_task_and_advances_from_claim(
    temp_db: HubDatabase,
) -> None:
    # A claim the seat already holds still reports success with the task id; the claim
    # step binds it and advances without branching on the already_claimed flag.
    run = SeatRun(temp_db, "developer")
    assert run.step == "claim"
    task = run.task(owner=SESSION_ID)
    ref = f"#{task.seq_num}"

    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": task.id},
        {"result": {"task_id": task.id, "ref": ref, "already_claimed": True}},
    )

    assert run.step == "route_skills"
    assert run.vars["assigned_task_id"] == task.id
    assert run.vars["assigned_task_ref"] == ref
