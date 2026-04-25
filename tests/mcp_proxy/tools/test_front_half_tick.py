"""Tests for the front-half conductor task-op tool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._expansion import _background_run_tasks
from gobby.mcp_proxy.tools.tasks._front_half import (
    _STAGE_LABELS,
    FRONT_HALF_COMPLETE_LABEL,
    FRONT_HALF_LABEL,
    NEEDS_REQUIREMENTS_PREFIX,
    PLANNING_ROUND_LABEL_PREFIX,
    _artifact_paths,
    create_front_half_registry,
)
from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


@pytest.fixture
def task_manager(temp_db):
    return LocalTaskManager(temp_db)


@pytest.fixture
def sync_manager(task_manager, temp_dir):
    return TaskSyncManager(task_manager, temp_dir / "tasks.jsonl")


@pytest.fixture
def project(project_manager, temp_dir):
    return project_manager.create(name="front-half-project", repo_path=str(temp_dir))


@pytest.fixture
def test_session(session_manager, project):
    session = session_manager.register(
        project_id=project.id,
        source="test",
        external_id="front-half-session",
        machine_id="test-machine",
    )
    return session.id


@pytest.fixture
def front_half_registry(task_manager, sync_manager):
    ctx = RegistryContext(
        task_manager=task_manager,
        sync_manager=sync_manager,
        task_validator=None,
        config=None,
    )
    return create_front_half_registry(ctx)


@pytest.fixture(autouse=True)
async def clear_background_runs():
    _background_run_tasks.clear()
    yield
    pending = list(_background_run_tasks.values())
    for task in pending:
        if not task.done():
            task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _background_run_tasks.clear()


@pytest.fixture
def parent_task(task_manager, project):
    return task_manager.create_task(
        project_id=project.id,
        title="Ship front-half conductor",
        description="The task may reference supporting plan docs.",
        task_type="feature",
        category="planning",
    )


def _stage_task(task_manager: LocalTaskManager, parent_id: str, stage: str):
    tasks = task_manager.list_tasks(
        parent_task_id=parent_id,
        label=_STAGE_LABELS[stage],
        limit=10,
        sort_by="updated_at",
        sort_order="desc",
    )
    assert tasks
    return tasks[0]


def _task_ident(parent_task) -> str:
    ident = str(parent_task.seq_num) if parent_task.seq_num is not None else parent_task.id[:8]
    return ident


def _plan_file_rel(parent_task, slug: str = "front-half-conductor") -> str:
    return f".gobby/plans/task-{_task_ident(parent_task)}-{slug}.md"


def _plan_file_path(repo_root: Path, parent_task, slug: str = "front-half-conductor") -> Path:
    return repo_root / _plan_file_rel(parent_task, slug)


def _legacy_plan_file_path(repo_root: Path, parent_task) -> Path:
    return repo_root / ".gobby" / "plans" / f"task-{_task_ident(parent_task)}-plan.md"


def _seed_plan_artifact(task_manager: LocalTaskManager, session_id: str, parent_task) -> str:
    plan_file = _plan_file_rel(parent_task)
    parent_ref = f"#{parent_task.seq_num}" if parent_task.seq_num is not None else parent_task.id
    SessionVariableManager(task_manager.db).merge_variables(
        session_id,
        {"plan_parent_ref": parent_ref, "artifact_path": plan_file},
    )
    return plan_file


def _approve_requirements_and_seed_plan(
    task_manager: LocalTaskManager, requirements_task, session_id: str, parent_task
) -> str:
    _approve_stage_task(task_manager, requirements_task.id, notes="Locked")
    return _seed_plan_artifact(task_manager, session_id, parent_task)


def _write_plan_file(repo_root: Path, parent_task) -> Path:
    plan_file = _plan_file_path(repo_root, parent_task)
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(
        "# Plan\n\n## Phase 1: Foundation\n\n- Create the first implementation task.\n",
        encoding="utf-8",
    )
    return plan_file


def _approve_stage_task(
    task_manager: LocalTaskManager, task_id: str, notes: str = "Approved"
) -> None:
    task_manager.mark_task_needs_review(task_id, review_notes="Ready for review")
    task_manager.mark_task_review_approved(task_id, approval_notes=notes)


class TestFrontHalfTick:
    def test_artifact_paths_returns_none_without_persisted_plan(
        self,
        task_manager,
        sync_manager,
        parent_task,
    ) -> None:
        ctx = RegistryContext(
            task_manager=task_manager,
            sync_manager=sync_manager,
            task_validator=None,
            config=None,
        )

        artifacts = _artifact_paths(ctx, parent_task)

        assert artifacts["plan_file"] is None

    def test_artifact_paths_uses_persisted_interactive_slug_path(
        self,
        task_manager,
        sync_manager,
        parent_task,
        test_session,
    ) -> None:
        ctx = RegistryContext(
            task_manager=task_manager,
            sync_manager=sync_manager,
            task_validator=None,
            config=None,
        )
        expected = _seed_plan_artifact(task_manager, test_session, parent_task)

        artifacts = _artifact_paths(ctx, parent_task)

        assert artifacts["plan_file"] == expected
        assert not expected.endswith("-plan.md")

    def test_artifact_paths_finds_existing_legacy_autonomous_plan(
        self,
        task_manager,
        sync_manager,
        parent_task,
        temp_dir,
    ) -> None:
        ctx = RegistryContext(
            task_manager=task_manager,
            sync_manager=sync_manager,
            task_validator=None,
            config=None,
        )
        plan_file = _legacy_plan_file_path(temp_dir, parent_task)
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text("# Legacy plan\n", encoding="utf-8")

        artifacts = _artifact_paths(ctx, parent_task)

        assert artifacts["plan_file"] == str(plan_file.relative_to(temp_dir))

    @pytest.mark.asyncio
    async def test_creates_requirements_stage_and_waits(
        self,
        front_half_registry,
        task_manager,
        parent_task,
        test_session,
    ) -> None:
        with session_context_for_test(test_session):
            result = await front_half_registry.call(
                "front_half_tick",
                {"task_id": parent_task.id},
            )

        assert result["success"] is True
        assert result["current_stage"] == "requirements"
        assert result["next_action"] == "wait_for_requirements_lock"
        requirements_task = _stage_task(task_manager, parent_task.id, "requirements")
        assert requirements_task.title.startswith("Requirements lock")
        assert FRONT_HALF_LABEL in (requirements_task.labels or [])

        refreshed_parent = task_manager.get_task(parent_task.id)
        assert refreshed_parent is not None
        assert FRONT_HALF_LABEL in (refreshed_parent.labels or [])

    @pytest.mark.asyncio
    async def test_requirements_approval_unlocks_planner_dispatch(
        self,
        front_half_registry,
        task_manager,
        parent_task,
        test_session,
    ) -> None:
        with session_context_for_test(test_session):
            await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        requirements_task = _stage_task(task_manager, parent_task.id, "requirements")
        expected_plan_file = _approve_requirements_and_seed_plan(
            task_manager, requirements_task, test_session, parent_task
        )

        with session_context_for_test(test_session):
            result = await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        assert result["current_stage"] == "planning"
        assert result["next_action"] == "spawn_planner"
        assert result["dispatch"]["agent"] == "planner"
        assert result["artifacts"]["plan_file"] == expected_plan_file

        refreshed_requirements = task_manager.get_task(requirements_task.id)
        assert refreshed_requirements is not None
        assert refreshed_requirements.status == "closed"

        planning_task = _stage_task(task_manager, parent_task.id, "planning")
        assert f"{PLANNING_ROUND_LABEL_PREFIX}0" in (planning_task.labels or [])

    @pytest.mark.asyncio
    async def test_requirements_approval_without_plan_path_fails_closed(
        self,
        front_half_registry,
        task_manager,
        parent_task,
        test_session,
    ) -> None:
        with session_context_for_test(test_session):
            await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        requirements_task = _stage_task(task_manager, parent_task.id, "requirements")
        _approve_stage_task(task_manager, requirements_task.id, notes="Locked")

        with session_context_for_test(test_session):
            result = await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        assert result["current_stage"] == "planning"
        assert result["next_action"] == "front_half_failed"
        assert result["artifacts"]["plan_file"] is None

    @pytest.mark.asyncio
    async def test_planning_needs_review_dispatches_adversary(
        self,
        front_half_registry,
        task_manager,
        parent_task,
        test_session,
    ) -> None:
        with session_context_for_test(test_session):
            await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        requirements_task = _stage_task(task_manager, parent_task.id, "requirements")
        _approve_requirements_and_seed_plan(
            task_manager, requirements_task, test_session, parent_task
        )

        with session_context_for_test(test_session):
            await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        planning_task = _stage_task(task_manager, parent_task.id, "planning")
        task_manager.mark_task_needs_review(planning_task.id, review_notes="Plan ready")

        with session_context_for_test(test_session):
            result = await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        assert result["current_stage"] == "planning"
        assert result["next_action"] == "spawn_plan_adversary"
        assert result["dispatch"]["agent"] == "plan-adversary"
        assert "Display round: 1" in result["dispatch"]["prompt"]

    @pytest.mark.asyncio
    async def test_planning_review_rejection_resumes_next_round(
        self,
        front_half_registry,
        task_manager,
        parent_task,
        test_session,
    ) -> None:
        with session_context_for_test(test_session):
            await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        requirements_task = _stage_task(task_manager, parent_task.id, "requirements")
        _approve_requirements_and_seed_plan(
            task_manager, requirements_task, test_session, parent_task
        )

        with session_context_for_test(test_session):
            await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        planning_task = _stage_task(task_manager, parent_task.id, "planning")
        task_manager.mark_task_needs_review(planning_task.id, review_notes="Ready")
        task_manager.mark_task_review_rejected(
            planning_task.id,
            rejection_notes="Sequencing is weak",
            round_number=1,
        )

        with session_context_for_test(test_session):
            result = await front_half_registry.call(
                "front_half_tick",
                {"task_id": parent_task.id, "max_planning_rounds": 3},
            )

        assert result["next_action"] == "spawn_planner"
        assert result["planning_round"] == 1

        refreshed = task_manager.get_task(planning_task.id)
        assert refreshed is not None
        assert refreshed.status == "open"
        assert f"{PLANNING_ROUND_LABEL_PREFIX}1" in (refreshed.labels or [])

    @pytest.mark.asyncio
    async def test_planning_budget_exhaustion_returns_failure(
        self,
        front_half_registry,
        task_manager,
        parent_task,
        test_session,
    ) -> None:
        with session_context_for_test(test_session):
            await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        requirements_task = _stage_task(task_manager, parent_task.id, "requirements")
        _approve_requirements_and_seed_plan(
            task_manager, requirements_task, test_session, parent_task
        )

        with session_context_for_test(test_session):
            await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        planning_task = _stage_task(task_manager, parent_task.id, "planning")
        task_manager.update_task(
            planning_task.id,
            labels=[FRONT_HALF_LABEL, _STAGE_LABELS["planning"], f"{PLANNING_ROUND_LABEL_PREFIX}2"],
        )
        task_manager.mark_task_needs_review(planning_task.id, review_notes="Ready")
        task_manager.mark_task_review_rejected(
            planning_task.id,
            rejection_notes="Still missing critical constraints",
            round_number=3,
        )

        with session_context_for_test(test_session):
            result = await front_half_registry.call(
                "front_half_tick",
                {"task_id": parent_task.id, "max_planning_rounds": 3},
            )

        assert result["next_action"] == "front_half_failed"
        refreshed = task_manager.get_task(planning_task.id)
        assert refreshed is not None
        assert refreshed.status == "open"
        assert f"{PLANNING_ROUND_LABEL_PREFIX}3" in (refreshed.labels or [])

    @pytest.mark.asyncio
    async def test_planning_approval_starts_expansion_run(
        self,
        front_half_registry,
        task_manager,
        parent_task,
        project,
        test_session,
        temp_dir,
    ) -> None:
        with session_context_for_test(test_session):
            await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        requirements_task = _stage_task(task_manager, parent_task.id, "requirements")
        _approve_requirements_and_seed_plan(
            task_manager, requirements_task, test_session, parent_task
        )

        with session_context_for_test(test_session):
            await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        planning_task = _stage_task(task_manager, parent_task.id, "planning")
        _approve_stage_task(task_manager, planning_task.id, notes="Approved")
        _write_plan_file(temp_dir, parent_task)

        run_manager = LocalExpansionRunManager(task_manager.db)
        with patch(
            "gobby.mcp_proxy.tools.tasks._front_half._execute_run_background",
            new=AsyncMock(return_value=None),
        ):
            with session_context_for_test(test_session):
                result = await front_half_registry.call(
                    "front_half_tick", {"task_id": parent_task.id}
                )
            await asyncio.sleep(0)

        assert result["current_stage"] == "expansion"
        assert result["next_action"] == "wait_for_expansion"
        latest_run = run_manager.get_latest_for_task(parent_task.id)
        assert latest_run is not None
        assert latest_run.plan_file == result["artifacts"]["plan_file"]
        assert latest_run.input_source == "plan"

    @pytest.mark.asyncio
    async def test_valid_completed_expansion_unlocks_test_architecture(
        self,
        front_half_registry,
        task_manager,
        parent_task,
        test_session,
        temp_dir,
    ) -> None:
        with session_context_for_test(test_session):
            await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        requirements_task = _stage_task(task_manager, parent_task.id, "requirements")
        _approve_requirements_and_seed_plan(
            task_manager, requirements_task, test_session, parent_task
        )

        with session_context_for_test(test_session):
            await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        planning_task = _stage_task(task_manager, parent_task.id, "planning")
        _approve_stage_task(task_manager, planning_task.id, notes="Approved")
        plan_file = _write_plan_file(temp_dir, parent_task)

        run_manager = LocalExpansionRunManager(task_manager.db)
        run = run_manager.create(
            parent_task_id=parent_task.id,
            project_id=parent_task.project_id,
            triggering_session_id=test_session,
            input_source="plan",
            plan_file=str(plan_file.relative_to(temp_dir)),
        )
        run_manager.save_compiled_spec(run.id, {"phases": [], "tasks": [], "dependencies": []})
        child = task_manager.create_task(
            project_id=parent_task.project_id,
            title="Implementation task",
            parent_task_id=parent_task.id,
            task_type="task",
            category="code",
            validation_criteria="Implementation exists.",
        )
        run_manager.save_apply_result(
            run.id,
            task_id_map={"task-1": child.id},
            created_task_ids=[child.id],
            completed=True,
        )

        mock_service = MagicMock()
        mock_service.validate_compiled_spec.return_value = {"valid": True}
        mock_service.validate_applied_run.return_value = {"valid": True}

        with patch(
            "gobby.mcp_proxy.tools.tasks._front_half._build_expansion_service",
            return_value=mock_service,
        ):
            with session_context_for_test(test_session):
                result = await front_half_registry.call(
                    "front_half_tick", {"task_id": parent_task.id}
                )

        assert result["current_stage"] == "test_architecture"
        assert result["next_action"] == "spawn_test_architect"
        assert result["dispatch"]["agent"] == "test-architect"

        expansion_task = _stage_task(task_manager, parent_task.id, "expansion")
        assert expansion_task.status == "closed"

    @pytest.mark.asyncio
    async def test_review_approved_test_architecture_marks_front_half_complete(
        self,
        front_half_registry,
        task_manager,
        parent_task,
        test_session,
    ) -> None:
        requirements_task = task_manager.create_task(
            project_id=parent_task.project_id,
            title="Requirements lock",
            parent_task_id=parent_task.id,
            category="planning",
            labels=[FRONT_HALF_LABEL, _STAGE_LABELS["requirements"]],
        )
        planning_task = task_manager.create_task(
            project_id=parent_task.project_id,
            title="Implementation plan",
            parent_task_id=parent_task.id,
            category="planning",
            labels=[
                FRONT_HALF_LABEL,
                _STAGE_LABELS["planning"],
                f"{PLANNING_ROUND_LABEL_PREFIX}0",
            ],
        )
        expansion_task = task_manager.create_task(
            project_id=parent_task.project_id,
            title="Task expansion",
            parent_task_id=parent_task.id,
            category="planning",
            labels=[FRONT_HALF_LABEL, _STAGE_LABELS["expansion"]],
        )
        test_architecture_task = task_manager.create_task(
            project_id=parent_task.project_id,
            title="Test architecture",
            parent_task_id=parent_task.id,
            category="planning",
            labels=[FRONT_HALF_LABEL, _STAGE_LABELS["test_architecture"]],
        )

        _approve_stage_task(task_manager, requirements_task.id, notes="Locked")
        _seed_plan_artifact(task_manager, test_session, parent_task)
        task_manager.close_task(requirements_task.id, reason="done")
        _approve_stage_task(task_manager, planning_task.id, notes="Approved")
        task_manager.close_task(planning_task.id, reason="done")
        task_manager.close_task(expansion_task.id, reason="done")
        _approve_stage_task(task_manager, test_architecture_task.id, notes="Approved")

        with session_context_for_test(test_session):
            result = await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        assert result["front_half_complete"] is True
        assert result["next_action"] == "front_half_complete"

        refreshed_parent = task_manager.get_task(parent_task.id)
        assert refreshed_parent is not None
        assert FRONT_HALF_COMPLETE_LABEL in (refreshed_parent.labels or [])

    @pytest.mark.asyncio
    async def test_needs_requirements_escalation_waits_for_clarification(
        self,
        front_half_registry,
        task_manager,
        parent_task,
        test_session,
    ) -> None:
        with session_context_for_test(test_session):
            await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        requirements_task = _stage_task(task_manager, parent_task.id, "requirements")
        _approve_requirements_and_seed_plan(
            task_manager, requirements_task, test_session, parent_task
        )

        with session_context_for_test(test_session):
            await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        planning_task = _stage_task(task_manager, parent_task.id, "planning")
        task_manager.escalate_task(
            planning_task.id,
            reason=f"{NEEDS_REQUIREMENTS_PREFIX} acceptance criteria are still vague",
        )

        with session_context_for_test(test_session):
            result = await front_half_registry.call("front_half_tick", {"task_id": parent_task.id})

        assert result["current_stage"] == "planning"
        assert result["next_action"] == "wait_for_requirements_clarification"
