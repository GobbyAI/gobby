"""Planning seats receive assignments and can wait for their draft (#25248)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from gobby.workflows.pipeline_state import ExecutionStatus
from tests.workflows.test_runbook_pipeline import (
    PROJECT_ID,
    RUN_INPUTS,
    SEATS,
    RunbookAgentsProxy,
    _executor,
    _team_planning,
)
from tests.workflows.test_seat_step_loops import COORDINATOR_ID, SeatRun

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration

AGENTS = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows/agents"
TASK_TITLE = "Fleet scheduling"
PLANNING_REF = "#29991"
CHILD = {
    "id": "99999999-0000-4000-8000-000000000001",
    "task_id": "99999999-0000-4000-8000-000000000001",
    "ref": PLANNING_REF,
    "title": f"Plan {TASK_TITLE}",
    "task_type": "task",
    "category": "planning",
    "status": "open",
    "is_closed": False,
    "parent_task_id": RUN_INPUTS["task"],
}
PEERS = {seat: f"session-{seat}" for seat in SEATS}


class PlanningProxy(RunbookAgentsProxy):
    """Mock external tools while executing the real bundled pipeline and templates."""

    def __init__(self, *, existing_child: bool = False) -> None:
        super().__init__()
        self.existing_child = existing_child

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None,
        enforce_workflow: bool,
    ) -> dict[str, Any]:
        if server == "gobby-tasks" or tool == "send_message":
            self.calls.append({"tool": f"{server}:{tool}", "arguments": arguments})
            if tool == "get_task":
                if arguments["task_id"] in (PLANNING_REF, CHILD["id"]):
                    return {"success": True, **CHILD}
                return {
                    "success": True,
                    "id": RUN_INPUTS["task"],
                    "ref": RUN_INPUTS["task"],
                    "title": TASK_TITLE,
                    "task_type": "epic",
                }
            if tool in ("list_tasks", "get_task_children"):
                children = [CHILD] if self.existing_child else []
                return {"success": True, "tasks": children, "children": children}
            if tool == "create_task":
                return {"success": True, **CHILD}
            if tool == "send_message":
                return {"success": True}
            raise AssertionError(f"Unhandled planning task tool: {tool}")
        reply = await super().call_tool(
            server, tool, arguments, session_id=session_id, enforce_workflow=enforce_workflow
        )
        if tool == "spawn_agent":
            reply["child_session_id"] = PEERS[arguments["agent"]]
        return reply


async def _launch(temp_db: HubDatabase, *, existing_child: bool = False) -> PlanningProxy:
    pipeline = await _team_planning(temp_db)
    proxy = PlanningProxy(existing_child=existing_child)
    result = await _executor(temp_db, proxy).execute(pipeline, RUN_INPUTS, PROJECT_ID)
    assert result.status == ExecutionStatus.COMPLETED
    return proxy


@pytest.mark.parametrize("seat", SEATS)
async def test_launch_prompt_names_task_title_and_plan_path(
    temp_db: HubDatabase, seat: str
) -> None:
    proxy = await _launch(temp_db)
    prompt = next(spawn["prompt"] for spawn in proxy.spawns() if spawn["agent"] == seat)
    assert RUN_INPUTS["task"] in prompt, f"{seat} needs the target task ref"
    assert TASK_TITLE in prompt, f"{seat} needs the target task title"
    assert re.search(r"\.gobby/plans/[^\s`\"']+\.md", prompt), f"{seat} needs a plan path"


@pytest.mark.parametrize("seat", SEATS[1:])
async def test_later_seats_receive_previously_launched_peer_sessions(
    temp_db: HubDatabase, seat: str
) -> None:
    proxy = await _launch(temp_db)
    prompt = next(spawn["prompt"] for spawn in proxy.spawns() if spawn["agent"] == seat)
    for prior in SEATS[: SEATS.index(seat)]:
        assert PEERS[prior] in prompt, f"{seat} needs {prior}'s session ref"


async def test_writer_learns_later_peer_sessions_without_operator_relay(
    temp_db: HubDatabase,
) -> None:
    proxy = await _launch(temp_db)
    notifications = [
        call["arguments"]["content"]
        for call in proxy.calls
        if call["tool"] == "gobby-agents:send_message"
        and call["arguments"].get("target") == "session"
        and call["arguments"].get("target_id") == PEERS["plan-writer"]
    ]
    for peer in SEATS[1:]:
        assert any(PEERS[peer] in message for message in notifications), peer


@pytest.mark.parametrize("existing_child", [False, True], ids=["create", "reuse"])
async def test_writer_gets_a_planning_child_to_claim_before_writing(
    temp_db: HubDatabase, existing_child: bool
) -> None:
    proxy = await _launch(temp_db, existing_child=existing_child)
    creates = [call for call in proxy.calls if call["tool"] == "gobby-tasks:create_task"]
    assert len(creates) == (0 if existing_child else 1)
    writer_call = next(
        call
        for call in proxy.calls
        if call["tool"] == "gobby-agents:spawn_agent"
        and call["arguments"]["agent"] == "plan-writer"
    )
    if creates:
        assert creates[0]["arguments"]["parent_task_id"] == RUN_INPUTS["task"]
        assert creates[0]["arguments"]["category"] == "planning"
        assert proxy.calls.index(creates[0]) < proxy.calls.index(writer_call)
    prompt = writer_call["arguments"]["prompt"]
    assert PLANNING_REF in prompt
    assert "claim_task" in prompt
    assert "before" in prompt.lower()


@pytest.mark.parametrize("seat", SEATS)
def test_seat_instructions_name_every_required_skill(seat: str) -> None:
    definition = yaml.safe_load((AGENTS / f"{seat}.yaml").read_text())
    prompt = "\n".join(definition["prompts"].values())
    required = definition["step_workflow"]["variables"]["required_skills"]
    for skill in required:
        assert skill in prompt, f"{seat}'s launch instructions omit required skill {skill}"


@pytest.mark.parametrize("seat", SEATS)
async def test_seat_can_wait_for_coordination_after_loading_skills(
    temp_db: HubDatabase, seat: str
) -> None:
    definition = yaml.safe_load((AGENTS / f"{seat}.yaml").read_text())
    run = SeatRun(temp_db, seat, definition=definition)
    await run.load(*run.vars["required_skills"])
    assert await run.admits(
        "gobby-agents",
        "wait_for_coordination",
        {"owner_session": COORDINATOR_ID, "reply": True},
    ), f"{seat} denies the runbook's idle wait in step {run.step}"
