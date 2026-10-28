"""Close authority: a Dev Manager closes with done; no-work reasons are Orchestrator-only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_close as lifecycle
import gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization as close_finalization
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.step_records import StepRequirements
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

NO_WORK_REASONS = ("moot", "superseded", "wont_do")
MISSING_STEPS = StepRequirements(
    required=("implementation", "closing"), missing=("implementation", "closing")
)


@dataclass(frozen=True, slots=True)
class Closing:
    db: HubDatabase
    project_id: str
    manager: LocalTaskManager
    context: RegistryContext
    task: Task


def _seat(db: HubDatabase, project_id: str, agent_type: str) -> str:
    session = SessionManager(db).register(
        external_id=f"close-authority-{agent_type}",
        machine_id=require_machine_id(),
        source="codex",
        project_id=project_id,
    )
    SessionVariableManager(db).set_variable(session.id, "_agent_type", agent_type)
    return session.id


@pytest.fixture
def closing(temp_db: HubDatabase, sample_git_project: dict[str, Any]) -> Closing:
    project_id = sample_git_project["id"]
    LocalProjectCheckoutManager(temp_db).register(
        require_machine_id(), project_id, str(sample_git_project["repo_path"])
    )
    manager = LocalTaskManager(temp_db)
    developer = _seat(temp_db, project_id, "developer")
    created = manager.create_task(
        project_id,
        "No-work candidate",
        category="code",
        task_type="task",
        implementation_domain="backend",
        validation_criteria="Close authority.",
    )
    task = manager.claim_task(created.id, developer)
    return Closing(temp_db, project_id, manager, RegistryContext(task_manager=manager), task)


async def _evaluate(closing: Closing, seat: str, reason: str) -> CloseEvaluation:
    with session_context_for_test(seat):
        return await lifecycle._evaluate_close(
            closing.context,
            task_id=closing.task.id,
            reason=reason,
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
            superseded_by="#25223",
        )


def _statuses(evaluation: CloseEvaluation) -> dict[int, str]:
    return {gate.item: gate.status for gate in evaluation.checklist.gates}


@pytest.mark.parametrize("reason", NO_WORK_REASONS)
@pytest.mark.asyncio
async def test_dev_manager_cannot_close_with_a_no_work_reason(
    closing: Closing, reason: str
) -> None:
    dev_manager = _seat(closing.db, closing.project_id, "dev-manager")

    evaluation = await _evaluate(closing, dev_manager, reason)

    assert not evaluation.ready
    assert "Orchestrator" in (evaluation.message or "")
    assert closing.manager.get_task(closing.task.id).closed_at is None


@pytest.mark.asyncio
async def test_dev_manager_done_close_runs_all_five_gates(
    closing: Closing, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "step_requirements", AsyncMock(return_value=MISSING_STEPS))
    dev_manager = _seat(closing.db, closing.project_id, "dev-manager")

    evaluation = await _evaluate(closing, dev_manager, "done")

    assert evaluation.error is None
    # Done keeps every gate: no linked commit fails gate 2, and the missing steps fail gate 5.
    statuses = _statuses(evaluation)
    assert sorted(statuses) == [1, 2, 3, 4, 5]
    assert statuses[2] == "failed"
    assert statuses[5] == "failed"
    assert not evaluation.ready


@pytest.mark.parametrize("reason", NO_WORK_REASONS)
@pytest.mark.asyncio
async def test_orchestrator_no_work_close_skips_commit_and_step_gates(
    closing: Closing, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    monkeypatch.setattr(lifecycle, "step_requirements", AsyncMock(return_value=MISSING_STEPS))
    orchestrator = _seat(closing.db, closing.project_id, "orchestrator")

    evaluation = await _evaluate(closing, orchestrator, reason)

    assert evaluation.error is None
    assert _statuses(evaluation) == {
        1: "passed",
        2: "skipped",
        3: "passed",
        4: "skipped",
        5: "skipped",
    }
    assert evaluation.ready


@pytest.mark.asyncio
async def test_orchestrator_no_work_close_still_waits_for_open_children(
    closing: Closing, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "step_requirements", AsyncMock(return_value=MISSING_STEPS))
    closing.manager.create_task(
        closing.project_id,
        "Open child",
        category="code",
        parent_task_id=closing.task.id,
        validation_criteria="Still open.",
    )
    orchestrator = _seat(closing.db, closing.project_id, "orchestrator")

    evaluation = await _evaluate(closing, orchestrator, "moot")

    assert _statuses(evaluation)[1] == "failed"
    assert not evaluation.ready


@pytest.mark.asyncio
async def test_orchestrator_no_work_close_records_its_reason_and_caller(
    closing: Closing, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "step_requirements", AsyncMock(return_value=MISSING_STEPS))
    orchestrator = _seat(closing.db, closing.project_id, "orchestrator")
    evaluation = await _evaluate(closing, orchestrator, "moot")
    assert evaluation.ready

    with session_context_for_test(orchestrator):
        await close_finalization.commit_close(
            closing.context,
            evaluation,
            reason="moot",
            changes_summary="Folded into another task.",
            commit_sha=None,
        )

    closed = closing.manager.get_task(closing.task.id)
    assert closed.closed_reason == "moot"
    assert closed.closed_in_session_id == orchestrator
