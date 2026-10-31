"""A claimed task never inherits the previous task's test gate.

Drives the bundled developer workflow through two successive tasks on one seat.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.step_records import StepSubmission, record_task_step
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import patch_local_machine_id
from tests.workflows.test_seat_step_loops import (
    AUTHORITY_MACHINE_ID,
    PEER_ID,
    PROJECT_ID,
    SESSION_ID,
    SHA,
    SeatRun,
)

pytestmark = pytest.mark.unit

DEVELOPER_YAML = (
    Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows/agents/developer.yaml"
)


def _bundled_developer() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(DEVELOPER_YAML.read_text())
    return loaded


@pytest.fixture
def seat(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> SeatRun:
    patch_local_machine_id(monkeypatch, AUTHORITY_MACHINE_ID)
    run = SeatRun(temp_db, "developer", definition=_bundled_developer())
    AgentDefinitionManager(temp_db).create(
        "inline-test-author",
        {
            "name": "inline-test-author",
            "prompts": {"agent": "Record test evidence."},
            "task_step_writes": ["test"],
            "workflows": {"rule_selectors": {"include": []}},
        },
        project_id=PROJECT_ID,
    )
    SessionVariableManager(temp_db).set_variable(PEER_ID, "_agent_type", "inline-test-author")
    return run


async def _claim(run: SeatRun, task: Task) -> None:
    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": task.id},
        {"result": {"task_id": task.id, "ref": f"#{task.seq_num}"}},
    )


async def _refresh(run: SeatRun, task: Task) -> None:
    with session_context_for_test(SESSION_ID):
        output = await create_task_registry(LocalTaskManager(run.db)).call(
            "get_task", {"task_id": task.id, "brief": False}
        )
    await run.call("gobby-tasks", "get_task", {"task_id": task.id}, output)


def _approve_committed_tests(run: SeatRun, task: Task) -> None:
    LocalTaskManager(run.db).link_commit(task.id, SHA)
    record_task_step(
        run.db,
        task_id=task.id,
        session_id=PEER_ID,
        step="test",
        submission=StepSubmission(
            status="approved",
            epoch=task.review_epoch,
            round_number=1,
            feedback="Committed failing acceptance tests; expected assertion failures inspected.",
            commit_sha=SHA,
        ),
    )


def _approve_moot_tests(run: SeatRun, task: Task) -> None:
    record_task_step(
        run.db,
        task_id=task.id,
        session_id=PEER_ID,
        step="test",
        submission=StepSubmission(
            status="approved",
            epoch=task.review_epoch,
            round_number=1,
            feedback="Existing tests cover every criterion; no new tests needed.",
            disposition="moot",
        ),
    )


async def _implement_and_close(run: SeatRun, task: Task) -> None:
    """Drive one approved task through its candidate and the Dev Manager's close."""
    await _refresh(run, task)
    assert run.step == "implement"
    await run.call(
        "gobby-tasks",
        "link_commit",
        {"task_id": task.id, "commit_sha": SHA},
        {"result": {"production_changes": True}},
    )
    assert run.step == "submit"
    await run.send(f"EVENT=CANDIDATE TASK=#{task.seq_num} RUN=run WT=tree COMMIT={SHA} NOTE=ready")
    await run.call(
        "gobby-tasks",
        "get_task",
        {"task_id": task.id},
        {"result": {"id": task.id, "ref": f"#{task.seq_num}", "state": {"is_closed": True}}},
    )
    assert run.step == "claim"


@pytest.mark.asyncio
async def test_second_claim_waits_for_its_own_test_record(seat: SeatRun) -> None:
    await seat.load(*seat.vars["required_skills"])
    assert seat.step == "claim"

    first = seat.task(owner=SESSION_ID)
    await _claim(seat, first)
    await _refresh(seat, first)
    assert seat.step == "await_tests"
    _approve_committed_tests(seat, first)
    await _refresh(seat, first)
    await _implement_and_close(seat, first)

    second = seat.task(owner=SESSION_ID)
    await _claim(seat, second)
    await _refresh(seat, second)
    assert seat.step == "await_tests"
    assert seat.vars["tests_ready"] is False
    assert not await seat.admits_native("Edit", {"file_path": "/repo/src/feature.py"})
    assert not await seat.admits_native("Bash", {"command": "touch src/feature.py"})

    _approve_moot_tests(seat, second)
    await _refresh(seat, second)
    assert seat.step == "implement"
