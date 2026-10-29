"""Seat close authority and Consolidator fold protections."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_close as lifecycle
import gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization as close_finalization
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._crud import create_crud_registry
from gobby.mcp_proxy.tools.tasks._lifecycle import create_lifecycle_registry
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.storage.definitions.agents import AgentDefinitionManager
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


def _seat(
    db: HubDatabase, project_id: str, agent_type: str, *, capabilities: tuple[str, ...] = ()
) -> str:
    session = SessionManager(db).register(
        external_id=f"close-authority-{agent_type}",
        machine_id=require_machine_id(),
        source="codex",
        project_id=project_id,
    )
    definitions = AgentDefinitionManager(db)
    if definitions.get_by_name(agent_type, project_id) is None:
        definitions.create(
            agent_type,
            {
                "name": agent_type,
                "prompts": {"agent": "Apply the declared task close policy."},
                "workflows": {"rule_selectors": {"include": []}},
                "capabilities": list(capabilities),
            },
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


@pytest.mark.parametrize("name", ["orchestrator", "custom-disposer"])
@pytest.mark.parametrize("granted", [True, False])
@pytest.mark.parametrize("reason", NO_WORK_REASONS)
@pytest.mark.asyncio
async def test_no_work_close_uses_declared_permission(
    closing: Closing, name: str, granted: bool, reason: str
) -> None:
    caller = _seat(
        closing.db,
        closing.project_id,
        name,
        capabilities=("close_without_work",) if granted else (),
    )

    evaluation = await _evaluate(closing, caller, reason)

    assert evaluation.ready is granted
    if not granted:
        assert evaluation.error == "no_work_close_forbidden"


@pytest.mark.parametrize("reason", NO_WORK_REASONS)
@pytest.mark.asyncio
async def test_dev_manager_cannot_close_with_a_no_work_reason(
    closing: Closing, reason: str
) -> None:
    dev_manager = _seat(closing.db, closing.project_id, "dev-manager")

    evaluation = await _evaluate(closing, dev_manager, reason)

    assert not evaluation.ready
    assert "close_without_work" in (evaluation.message or "")
    assert closing.manager.get_task(closing.task.id).closed_at is None


@pytest.mark.parametrize("case", ["operator", "spawned", "disabled"])
@pytest.mark.asyncio
async def test_no_work_operator_exemption_and_disabled_grant(closing: Closing, case: str) -> None:
    if case == "disabled":
        caller = _seat(
            closing.db, closing.project_id, "disposer", capabilities=("close_without_work",)
        )
        definitions = AgentDefinitionManager(closing.db)
        definition = definitions.get_by_name("disposer", closing.project_id)
        assert definition is not None
        definitions.update(definition.id, enabled=False)
    else:
        caller = (
            SessionManager(closing.db)
            .register(
                external_id="unseated-caller",
                machine_id=require_machine_id(),
                source="codex",
                project_id=closing.project_id,
                parent_session_id=closing.task.claimed_by_session_id if case == "spawned" else None,
                agent_depth=1 if case == "spawned" else 0,
            )
            .id
        )

    evaluation = await _evaluate(closing, caller, "moot")

    assert evaluation.ready is (case == "operator")
    if case != "operator":
        assert evaluation.error == "no_work_close_forbidden"


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
    orchestrator = _seat(
        closing.db, closing.project_id, "orchestrator", capabilities=("close_without_work",)
    )

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
    orchestrator = _seat(
        closing.db, closing.project_id, "orchestrator", capabilities=("close_without_work",)
    )

    evaluation = await _evaluate(closing, orchestrator, "moot")

    assert _statuses(evaluation)[1] == "failed"
    assert not evaluation.ready


@pytest.mark.asyncio
async def test_orchestrator_no_work_close_records_its_reason_and_caller(
    closing: Closing, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "step_requirements", AsyncMock(return_value=MISSING_STEPS))
    orchestrator = _seat(
        closing.db, closing.project_id, "orchestrator", capabilities=("close_without_work",)
    )
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


@pytest.mark.parametrize("reason", ("moot", "superseded"))
@pytest.mark.asyncio
async def test_consolidator_closes_unclaimed_task_without_commit(
    closing: Closing, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    closing.manager.release_task_claim(closing.task.id)
    monkeypatch.setattr(lifecycle, "step_requirements", AsyncMock(return_value=MISSING_STEPS))
    seat = _seat(closing.db, closing.project_id, "consolidator")
    close = create_lifecycle_registry(closing.context).get_tool("close_task")
    assert close is not None
    with session_context_for_test(seat):
        result = await close(
            task_id=closing.task.id,
            reason=reason,
            superseded_by="#25223" if reason == "superseded" else None,
            changes_summary="Orchestrator directed approved proposal 3.",
        )
    assert result.get("success") is True, result
    task = closing.manager.get_task(closing.task.id)
    assert task.closed_at is not None
    assert task.closed_reason == reason
    assert task.closed_in_session_id == seat
    assert not task.commits


@pytest.mark.parametrize("reason", ("done", "wont_do"))
@pytest.mark.asyncio
async def test_consolidator_cannot_use_other_close_reasons(closing: Closing, reason: str) -> None:
    closing.manager.release_task_claim(closing.task.id)
    seat = _seat(closing.db, closing.project_id, "consolidator")
    evaluation = await _evaluate(closing, seat, reason)
    assert evaluation.error is not None
    assert not evaluation.ready
    assert closing.manager.get_task(closing.task.id).closed_at is None


@pytest.mark.asyncio
async def test_consolidator_superseded_requires_survivor(closing: Closing) -> None:
    closing.manager.release_task_claim(closing.task.id)
    seat = _seat(closing.db, closing.project_id, "consolidator")
    with session_context_for_test(seat):
        evaluation = await lifecycle._evaluate_close(
            closing.context,
            task_id=closing.task.id,
            reason="superseded",
            superseded_by=None,
            commit_sha=None,
            project_path=None,
        )
    assert evaluation.error == "superseded_by_required"
    assert not evaluation.ready


@pytest.mark.asyncio
async def test_consolidator_no_work_close_preserves_child_gate(
    closing: Closing, monkeypatch: pytest.MonkeyPatch
) -> None:
    closing.manager.release_task_claim(closing.task.id)
    closing.manager.create_task(
        closing.project_id,
        "Unfinished child",
        parent_task_id=closing.task.id,
        category="code",
        validation_criteria="Still open.",
    )
    monkeypatch.setattr(lifecycle, "step_requirements", AsyncMock(return_value=MISSING_STEPS))
    seat = _seat(closing.db, closing.project_id, "consolidator")
    evaluation = await _evaluate(closing, seat, "moot")
    assert evaluation.error is None
    assert _statuses(evaluation)[1] == "failed"
    assert not evaluation.ready


@pytest.mark.asyncio
async def test_consolidator_no_work_close_preserves_cleanliness_gate(
    closing: Closing, sample_git_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    closing.manager.release_task_claim(closing.task.id)
    monkeypatch.setattr(lifecycle, "step_requirements", AsyncMock(return_value=MISSING_STEPS))
    seat = _seat(closing.db, closing.project_id, "consolidator")
    repo = Path(sample_git_project["repo_path"])
    (repo / "unfinished.py").write_text("unfinished = True\n")
    SessionVariableManager(closing.db).merge_variables(
        seat,
        {
            "task_edited_files": {closing.task.id: ["unfinished.py"]},
            "task_edited_file_checkouts": {closing.task.id: {str(repo): ["unfinished.py"]}},
        },
    )
    evaluation = await _evaluate(closing, seat, "moot")
    assert evaluation.error is None
    assert _statuses(evaluation)[3] == "failed"
    assert not evaluation.ready
    assert closing.manager.get_task(closing.task.id).closed_at is None


@pytest.mark.parametrize("protection", ("foreign_claim", "deferred-by-josh", "22978", "22225"))
@pytest.mark.parametrize("operation", ("update_task", "close_task"))
@pytest.mark.asyncio
async def test_consolidator_cannot_mutate_protected_tasks(
    closing: Closing, protection: str, operation: str
) -> None:
    if protection != "foreign_claim":
        closing.manager.release_task_claim(closing.task.id)
    if protection == "deferred-by-josh":
        closing.manager.update_task(closing.task.id, labels=["deferred-by-josh"])
    elif protection in ("22978", "22225"):
        root = closing.manager.create_task(
            closing.project_id,
            "Protected root",
            task_type="epic",
            category="code",
            validation_criteria="Protected operator subtree.",
        )
        # Set the operator root identity only in this isolated test database.
        with closing.db.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET seq_num = %s, path_cache = %s WHERE id = %s",
                (int(protection), protection, root.id),
            )
        parent = closing.manager.create_task(
            closing.project_id,
            "Intermediate epic",
            task_type="epic",
            category="code",
            parent_task_id=root.id,
            validation_criteria="Nested protection.",
        )
        closing.manager.update_task(closing.task.id, parent_task_id=parent.id)
    before = closing.manager.get_task(closing.task.id)
    seat = _seat(closing.db, closing.project_id, "consolidator")
    with session_context_for_test(seat):
        if operation == "update_task":
            update = create_crud_registry(closing.context).get_tool("update_task")
            assert update is not None
            result = update(
                task_id=closing.task.id, validation_criteria="Unauthorized replacement."
            )
            assert result.get("error"), result
        else:
            evaluation = await _evaluate(closing, seat, "moot")
            assert evaluation.error is not None
            assert not evaluation.ready
    after = closing.manager.get_task(closing.task.id)
    assert after.validation_criteria == before.validation_criteria
    assert after.closed_at is None
    assert after.claimed_by_session_id == before.claimed_by_session_id


def test_consolidator_updates_unprotected_survivor(closing: Closing) -> None:
    closing.manager.release_task_claim(closing.task.id)
    seat = _seat(closing.db, closing.project_id, "consolidator")
    update = create_crud_registry(closing.context).get_tool("update_task")
    assert update is not None
    with session_context_for_test(seat):
        result = update(
            task_id=closing.task.id, validation_criteria="Original and folded criteria."
        )
    assert not result.get("error"), result
    assert closing.manager.get_task(closing.task.id).validation_criteria == (
        "Original and folded criteria."
    )


def _protected_epic(closing: Closing, protection: str, developer: str) -> str:
    """An epic guarded by a deferred-by-josh label or a claim by the developer."""
    epic = closing.manager.create_task(
        closing.project_id,
        "Protected epic",
        task_type="epic",
        category="code",
        validation_criteria="Protected ancestor.",
    )
    if protection == "deferred-by-josh":
        closing.manager.update_task(epic.id, labels=["deferred-by-josh"])
    else:
        closing.manager.claim_task(epic.id, developer)
    return epic.id


@pytest.mark.parametrize("protection", ("deferred-by-josh", "foreign_claim"))
@pytest.mark.parametrize("operation", ("update_task", "close_task"))
@pytest.mark.asyncio
async def test_consolidator_cannot_mutate_under_protected_ancestor(
    closing: Closing, monkeypatch: pytest.MonkeyPatch, protection: str, operation: str
) -> None:
    developer = closing.task.claimed_by_session_id
    assert developer is not None
    closing.manager.release_task_claim(closing.task.id)
    epic_id = _protected_epic(closing, protection, developer)
    closing.manager.update_task(closing.task.id, parent_task_id=epic_id)
    monkeypatch.setattr(lifecycle, "step_requirements", AsyncMock(return_value=MISSING_STEPS))
    before = closing.manager.get_task(closing.task.id)
    seat = _seat(closing.db, closing.project_id, "consolidator")
    with session_context_for_test(seat):
        if operation == "update_task":
            update = create_crud_registry(closing.context).get_tool("update_task")
            assert update is not None
            result = update(
                task_id=closing.task.id, validation_criteria="Unauthorized replacement."
            )
            assert result.get("error"), result
        else:
            close = create_lifecycle_registry(closing.context).get_tool("close_task")
            assert close is not None
            result = await close(
                task_id=closing.task.id,
                reason="moot",
                changes_summary="Orchestrator directed approved proposal 3.",
            )
            assert not result.get("success"), result
    after = closing.manager.get_task(closing.task.id)
    assert after.validation_criteria == before.validation_criteria
    assert after.closed_at is None
    assert closing.manager.get_task(epic_id).closed_at is None


@pytest.mark.parametrize("protection", ("deferred-by-josh", "foreign_claim"))
@pytest.mark.parametrize("direction", ("into", "out_of"))
@pytest.mark.asyncio
async def test_consolidator_cannot_reparent_across_protected_parent(
    closing: Closing, protection: str, direction: str
) -> None:
    developer = closing.task.claimed_by_session_id
    assert developer is not None
    closing.manager.release_task_claim(closing.task.id)
    protected_id = _protected_epic(closing, protection, developer)
    if direction == "into":
        destination_id = protected_id
    else:
        closing.manager.update_task(closing.task.id, parent_task_id=protected_id)
        destination_id = closing.manager.create_task(
            closing.project_id,
            "Free epic",
            task_type="epic",
            category="code",
            validation_criteria="Unprotected parent.",
        ).id
    before = closing.manager.get_task(closing.task.id)
    seat = _seat(closing.db, closing.project_id, "consolidator")
    update = create_crud_registry(closing.context).get_tool("update_task")
    assert update is not None
    with session_context_for_test(seat):
        result = update(task_id=closing.task.id, parent_task_id=destination_id)
    assert result.get("error"), result
    assert closing.manager.get_task(closing.task.id).parent_task_id == before.parent_task_id
