"""Per-phase LLM compile path for multi-phase plan expansions."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.expansion_service import ExpansionService

pytestmark = pytest.mark.unit


@pytest.fixture
def task_manager(temp_db: Any) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


@pytest.fixture
def run_manager(temp_db: Any) -> LocalExpansionRunManager:
    return LocalExpansionRunManager(temp_db)


@pytest.fixture
def service(
    task_manager: LocalTaskManager, run_manager: LocalExpansionRunManager
) -> ExpansionService:
    return ExpansionService(
        task_manager=task_manager, llm_service=MagicMock(), run_manager=run_manager
    )


def _write_plan(tmp_dir: Path, content: str) -> Path:
    path = tmp_dir / "plan.md"
    path.write_text(content)
    return path


def _phase_spec(phase_id: str, title: str, task_titles: list[str]) -> dict[str, Any]:
    """Build a minimal raw compile spec for a single phase."""
    tasks = [
        {
            "id": f"{phase_id}-t{i + 1}",
            "phase_id": phase_id,
            "title": title_i,
            "description": f"Body of {title_i}",
            "category": "code",
            "validation": f"{title_i} is complete",
        }
        for i, title_i in enumerate(task_titles)
    ]
    return {
        "phases": [
            {
                "id": phase_id,
                "title": title,
                "summary": f"Summary for {title}",
                "test_intent": {
                    "summary": f"Verify {title}",
                    "behaviors": ["b1"],
                    "suggested_test_files": [],
                    "entry_criteria": [],
                },
            }
        ],
        "tasks": tasks,
        "dependencies": [],
    }


class TestCompileRunDispatch:
    @pytest.mark.asyncio
    async def test_multi_phase_plan_calls_llm_per_phase(
        self,
        service: ExpansionService,
        task_manager: LocalTaskManager,
        run_manager: LocalExpansionRunManager,
        sample_project: dict,
        tmp_path: Path,
    ) -> None:
        plan = _write_plan(
            tmp_path,
            "# Epic\n\n"
            "## Phase 0: Prereqs\n\nPrep body.\n\n"
            "## Phase 1: Build\n\nBuild body.\n\n"
            "## Phase 2: Ship\n\nShip body.\n",
        )
        parent = task_manager.create_task(project_id=sample_project["id"], title="Root")
        run = run_manager.create(
            parent_task_id=parent.id,
            project_id=sample_project["id"],
            triggering_session_id=None,
            input_source="plan",
            plan_file=str(plan),
        )

        per_phase_specs = [
            _phase_spec("p-prereqs", "Prereqs", ["Fix A", "Fix B"]),
            _phase_spec("p-build", "Build", ["Add X"]),
            _phase_spec("p-ship", "Ship", ["Release Y"]),
        ]
        fake_call = AsyncMock(side_effect=per_phase_specs)
        single_call = AsyncMock()
        with (
            patch.object(service, "_generate_raw_spec_for_phase", fake_call),
            patch.object(service, "_generate_raw_spec", single_call),
        ):
            refreshed = await service.compile_run(run.id)

        assert fake_call.await_count == 3
        assert single_call.await_count == 0

        # Each call receives the matching phase section body
        phase_numbers = [call.args[2]["number"] for call in fake_call.await_args_list]
        assert phase_numbers == [0, 1, 2]

        compiled = refreshed.compiled_spec
        assert compiled is not None
        phase_ids = {p["id"] for p in compiled["phases"]}
        assert phase_ids == {
            "phase-0-p-prereqs",
            "phase-1-p-build",
            "phase-2-p-ship",
        }
        task_ids = {t["id"] for t in compiled["tasks"]}
        assert len(task_ids) == 4  # 2 + 1 + 1
        # IDs from different phases are prefixed disjointly
        assert "phase-0-p-prereqs-t1" in task_ids
        assert "phase-1-p-build-t1" in task_ids
        assert "phase-2-p-ship-t1" in task_ids

    @pytest.mark.asyncio
    async def test_single_phase_plan_uses_single_call_path(
        self,
        service: ExpansionService,
        task_manager: LocalTaskManager,
        run_manager: LocalExpansionRunManager,
        sample_project: dict,
        tmp_path: Path,
    ) -> None:
        plan = _write_plan(
            tmp_path,
            "# Epic\n\n## Phase 1: Only\n\nOnly body.\n",
        )
        parent = task_manager.create_task(project_id=sample_project["id"], title="Root")
        run = run_manager.create(
            parent_task_id=parent.id,
            project_id=sample_project["id"],
            triggering_session_id=None,
            input_source="plan",
            plan_file=str(plan),
        )

        single_call = AsyncMock(return_value=_phase_spec("p-only", "Only", ["Do X"]))
        phase_call = AsyncMock()
        with (
            patch.object(service, "_generate_raw_spec", single_call),
            patch.object(service, "_generate_raw_spec_for_phase", phase_call),
        ):
            refreshed = await service.compile_run(run.id)

        assert single_call.await_count == 1
        assert phase_call.await_count == 0
        assert refreshed.compiled_spec is not None

    @pytest.mark.asyncio
    async def test_no_plan_file_uses_single_call_path(
        self,
        service: ExpansionService,
        task_manager: LocalTaskManager,
        run_manager: LocalExpansionRunManager,
        sample_project: dict,
    ) -> None:
        parent = task_manager.create_task(project_id=sample_project["id"], title="Root")
        run = run_manager.create(
            parent_task_id=parent.id,
            project_id=sample_project["id"],
            triggering_session_id=None,
            input_source="task",
        )
        single_call = AsyncMock(return_value=_phase_spec("p-1", "Phase", ["Do X"]))
        phase_call = AsyncMock()
        with (
            patch.object(service, "_generate_raw_spec", single_call),
            patch.object(service, "_generate_raw_spec_for_phase", phase_call),
        ):
            await service.compile_run(run.id)
        assert single_call.await_count == 1
        assert phase_call.await_count == 0


class TestCompileMultiPhaseMaxSubtasks:
    @pytest.mark.asyncio
    async def test_per_phase_cap_applies_independently(
        self,
        service: ExpansionService,
        task_manager: LocalTaskManager,
        run_manager: LocalExpansionRunManager,
        sample_project: dict,
        tmp_path: Path,
    ) -> None:
        plan = _write_plan(
            tmp_path,
            "## Phase 0: A\n\nA.\n\n## Phase 1: B\n\nB.\n",
        )
        parent = task_manager.create_task(project_id=sample_project["id"], title="Root")
        run = run_manager.create(
            parent_task_id=parent.id,
            project_id=sample_project["id"],
            triggering_session_id=None,
            input_source="plan",
            plan_file=str(plan),
        )

        # Configure a very small cap that each phase still satisfies
        config = MagicMock()
        config.max_subtasks = 2
        config.prompt_path = None
        config.system_prompt_path = None
        config.provider = "claude"
        config.model = "opus"
        with patch.object(service, "_get_expansion_config", return_value=config):
            with (
                patch.object(
                    service,
                    "_generate_raw_spec_for_phase",
                    AsyncMock(
                        side_effect=[
                            _phase_spec("p-a", "A", ["T1", "T2"]),
                            _phase_spec("p-b", "B", ["T3", "T4"]),
                        ]
                    ),
                ),
            ):
                refreshed = await service.compile_run(run.id)
        compiled = refreshed.compiled_spec
        assert compiled is not None
        # Cumulative total (4) exceeds per-phase cap (2) but each phase is at the cap,
        # so the compile succeeds — proves cap is per-phase, not cumulative.
        assert len(compiled["tasks"]) == 4

    @pytest.mark.asyncio
    async def test_per_phase_cap_fires_with_phase_scoped_error(
        self,
        service: ExpansionService,
        task_manager: LocalTaskManager,
        run_manager: LocalExpansionRunManager,
        sample_project: dict,
        tmp_path: Path,
    ) -> None:
        plan = _write_plan(
            tmp_path,
            "## Phase 0: A\n\nA.\n\n## Phase 1: B\n\nB.\n",
        )
        parent = task_manager.create_task(project_id=sample_project["id"], title="Root")
        run = run_manager.create(
            parent_task_id=parent.id,
            project_id=sample_project["id"],
            triggering_session_id=None,
            input_source="plan",
            plan_file=str(plan),
        )

        config = MagicMock()
        config.max_subtasks = 1
        config.prompt_path = None
        config.system_prompt_path = None
        config.provider = "claude"
        config.model = "opus"
        with patch.object(service, "_get_expansion_config", return_value=config):
            with patch.object(
                service,
                "_generate_raw_spec_for_phase",
                AsyncMock(
                    side_effect=[
                        _phase_spec("p-a", "A", ["T1"]),
                        _phase_spec("p-b", "B", ["T3", "T4"]),  # two > cap of 1
                    ]
                ),
            ):
                with pytest.raises(ValueError, match=r"Phase 1.*exceeds max_subtasks"):
                    await service.compile_run(run.id)

    @pytest.mark.asyncio
    async def test_empty_phase_spec_is_rejected(
        self,
        service: ExpansionService,
        task_manager: LocalTaskManager,
        run_manager: LocalExpansionRunManager,
        sample_project: dict,
        tmp_path: Path,
    ) -> None:
        plan = _write_plan(
            tmp_path,
            "## Phase 0: A\n\nA.\n\n## Phase 1: B\n\nB.\n",
        )
        parent = task_manager.create_task(project_id=sample_project["id"], title="Root")
        run = run_manager.create(
            parent_task_id=parent.id,
            project_id=sample_project["id"],
            triggering_session_id=None,
            input_source="plan",
            plan_file=str(plan),
        )

        with patch.object(
            service,
            "_generate_raw_spec_for_phase",
            AsyncMock(
                side_effect=[
                    _phase_spec("p-a", "A", ["T1"]),
                    _phase_spec("p-b", "B", []),
                ]
            ),
        ):
            with pytest.raises(ValueError, match=r"Phase 1 spec produced no tasks"):
                await service.compile_run(run.id)
