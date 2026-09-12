"""Inline narratives travel with existing planning and durable work units."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.memory.dream.cron import reconcile_interrupted_dream_runs
from gobby.memory.dream.options import DreamRunOptions
from gobby.memory.dream.planner import build_raw_plan
from gobby.memory.dream.related import RelatedEvidenceSession
from gobby.memory.dream.service import MemoryDreamService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.memories_scope import MemoryScope
from gobby.storage.projects import PERSONAL_PROJECT_ID, LocalProjectManager
from tests.memory.test_dream import (
    _as_dream_manager,
    _candidate,
    _FakeDreamDB,
    _FakeSweepManager,
    _planner_db,
    _row,
    _sweep_config,
)


@pytest.mark.parametrize("malformed", [None, 123, [], {}, ""])
async def test_pages_preserve_actions_and_previous_bounded_summary(malformed: object) -> None:
    planner = AsyncMock(
        side_effect=[
            {"actions": [{"action": "keep", "memory_id": "m0"}], "summary": "x" * 1100},
            {"actions": [{"action": "keep", "memory_id": "m1"}], "summary": malformed},
            {"actions": [{"action": "keep", "memory_id": "m2"}], "summary": "Retain preferences."},
        ]
    )
    with patch("gobby.memory.dream.planner._call_llm_planner", planner):
        plan = await build_raw_plan(
            candidates=[_candidate(f"m{i}") for i in range(3)],
            dream_config=SimpleNamespace(planner_batch_size=1),
            llm_service=MagicMock(),
            db=_planner_db(),
            project_id="proj-1",
            skip_consolidation=False,
            previous_summary="Earlier themes.",
        )
    assert [call.kwargs["previous_summary"] for call in planner.call_args_list] == [
        "Earlier themes.",
        "x" * 1000,
        "x" * 1000,
    ]
    assert [action["memory_id"] for action in plan["actions"]] == ["m0", "m1", "m2"]
    assert plan["narrative"] == "Retain preferences."
    assert plan["planner_errors"] == []


@pytest.mark.parametrize("dry_run", [False, True])
async def test_work_units_checkpoint_narrative_without_claiming_mutations(dry_run: bool) -> None:
    db = _FakeDreamDB()
    db.memories = {f"m{i}": _row(f"m{i}", f"content {i}") for i in range(3)}
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(_FakeSweepManager(db)),
        dream_config=_sweep_config(unit_size=1),
        llm_service=MagicMock(),
    )
    planner = AsyncMock(
        side_effect=[
            {"actions": [], "narrative": "Prefer retaining durable conventions."},
            {"actions": [], "narrative": None},
            {"actions": [], "narrative": "Retain conventions and preferences."},
        ]
    )
    with patch("gobby.memory.dream.orchestrator.build_raw_plan", planner):
        result = await service.run(DreamRunOptions(dry_run=dry_run, project_id="proj-1"))
    assert result["success"] is True
    run = result["run"]
    assert run["summary"]["narrative"] == "Retain conventions and preferences."
    assert run["summary"]["mutations"] == 0
    assert run["summary"]["candidates_reviewed"] == 3
    assert run["checkpoint"]["totals"]["narrative"] == run["summary"]["narrative"]
    assert [call.kwargs["previous_summary"] for call in planner.call_args_list] == [
        "",
        "Prefer retaining durable conventions.",
        "Prefer retaining durable conventions.",
    ]


async def test_resume_restores_narrative_and_counts_without_replaying_cohort() -> None:
    db = _FakeDreamDB()
    db.memories = {f"m{i}": _row(f"m{i}", f"content {i}") for i in range(3)}
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(_FakeSweepManager(db)),
        dream_config=_sweep_config(unit_size=1),
        llm_service=MagicMock(),
    )
    options = DreamRunOptions(dry_run=False, project_id="proj-1")
    started = await service.start_async(options)
    run_id = str(started["run_id"])
    related = RelatedEvidenceSession()
    first = await service._build_orchestrator(run_id, options, related)
    with patch(
        "gobby.memory.dream.orchestrator.build_raw_plan",
        AsyncMock(
            return_value={
                "actions": [],
                "narrative": "First cohort retained durable conventions.",
            }
        ),
    ):
        await first.run_unit()
    await related.aclose()
    resumed = await service._build_orchestrator(run_id, options, RelatedEvidenceSession())
    assert resumed.totals.candidates_reviewed == 1
    assert resumed.totals.narrative == "First cohort retained durable conventions."
    planner = AsyncMock(return_value={"actions": [], "narrative": "Conventions and preferences."})
    with patch("gobby.memory.dream.orchestrator.build_raw_plan", planner):
        totals = await resumed.run_sweep()
    await resumed.related_session.aclose()
    assert totals.candidates_reviewed == 3
    assert totals.mutations == 0
    assert planner.await_count == 2
    assert planner.call_args_list[0].kwargs["previous_summary"] == first.totals.narrative


async def test_failure_keeps_last_completed_narrative_and_outcomes() -> None:
    db = _FakeDreamDB()
    db.memories = {f"m{i}": _row(f"m{i}", f"content {i}") for i in range(2)}
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(_FakeSweepManager(db)),
        dream_config=_sweep_config(unit_size=1),
        llm_service=MagicMock(),
    )
    planner = AsyncMock(
        side_effect=[
            {"actions": [], "narrative": "Retain the durable convention."},
            {"actions": [], "planner_errors": ["provider unavailable"]},
        ]
    )
    with patch("gobby.memory.dream.orchestrator.build_raw_plan", planner):
        result = await service.run(DreamRunOptions(dry_run=False, project_id="proj-1"))
    assert result["success"] is False
    assert result["run"]["status"] == "failed"
    assert result["run"]["summary"]["narrative"] == "Retain the durable convention."
    assert result["run"]["summary"]["candidates_reviewed"] == 1
    assert result["run"]["summary"]["mutations"] == 0


async def test_aggregate_restart_reuses_scope_run_and_summary(temp_db: HubDatabase) -> None:
    manager = MagicMock(wraps=LocalMemoryManager(temp_db))
    manager.db = temp_db
    manager.notify_memory_changed = MagicMock()
    for index in range(3):
        manager.create_memory(content=f"durable preference {index}", project_id=PERSONAL_PROJECT_ID)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(unit_size=1),
        llm_service=MagicMock(),
    )
    started = await service.start_all_due_projects_async(dry_run=False)
    aggregate_id = str(started["run_id"])
    scope = MemoryScope.project_only(PERSONAL_PROJECT_ID)
    sweep = await service._open_scope_sweep(scope, memory_type=None, full_sweep=False)
    with patch(
        "gobby.memory.dream.orchestrator.build_raw_plan",
        AsyncMock(
            return_value={
                "actions": [],
                "narrative": "Retain durable preferences.",
            }
        ),
    ):
        await sweep.orchestrator.run_unit()
    service.store.update_run(
        aggregate_id, checkpoint={"scope_runs": {f"project:{PERSONAL_PROJECT_ID}": sweep.run_id}}
    )
    await sweep.related_session.aclose()
    reconcile_interrupted_dream_runs(_as_dream_manager(manager))
    resumed = await service.start_all_due_projects_async(dry_run=False)
    assert resumed["run_id"] == aggregate_id
    planner = AsyncMock(return_value={"actions": [], "narrative": "Retain universal preferences."})
    # Platform truth invalidation is independent of checkpoint restoration.
    with (
        patch.object(service, "_apply_platform_truth_change_trigger", AsyncMock()),
        patch("gobby.memory.dream.orchestrator.build_raw_plan", planner),
    ):
        result = await service.execute_all_due_projects_run(aggregate_id)
    assert result["status"] == "completed"
    assert len(result["aggregate"]["runs"]) == 1
    assert result["aggregate"]["runs"][0]["run_id"] == sweep.run_id
    assert planner.await_count == 2
    assert planner.call_args_list[0].kwargs["previous_summary"] == "Retain durable preferences."
    assert result["run"]["summary"]["noops"] == 3
    assert result["run"]["summary"]["mutations"] == 0
    assert result["run"]["summary"]["scope_summaries"][0]["narrative"] == (
        "Retain universal preferences."
    )


async def test_all_projects_receive_their_own_review_summary(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    projects = [LocalProjectManager(temp_db).create(name=name) for name in ("alpha", "beta")]
    roots = {project.id: str(tmp_path / project.name) for project in projects}
    manager = MagicMock(wraps=LocalMemoryManager(temp_db))
    manager.db = temp_db
    manager.notify_memory_changed = MagicMock()
    for project in projects:
        manager.create_memory(content=f"Durable {project.name} convention.", project_id=project.id)
    manager.create_memory(
        content="Universal preference.",
        project_id=projects[0].id,
        is_global=True,
    )
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(unit_size=2),
        llm_service=MagicMock(),
    )

    async def review(**kwargs: Any) -> dict[str, Any]:
        scope = kwargs["project_id"] or "global"
        return {"actions": [], "narrative": f"Retain durable conventions for {scope}."}

    planner = AsyncMock(side_effect=review)
    with (
        patch.object(service, "_apply_platform_truth_change_trigger", AsyncMock()),
        patch.object(service, "_resolve_repo_path", side_effect=roots.__getitem__),
        patch("gobby.memory.dream.orchestrator.build_raw_plan", planner),
    ):
        started = await service.start_all_due_projects_async(dry_run=False)
        result = await service.execute_all_due_projects_run(str(started["run_id"]))
    assert result["status"] == "completed"
    runs = result["aggregate"]["runs"]
    assert len(runs) == 3
    assert planner.await_count == 3
    assert {run["project_id"] for run in runs} == {None, *(project.id for project in projects)}
    for project in projects:
        run = next(run for run in runs if run["project_id"] == project.id)
        stored = service.store.get_run(run["run_id"])
        assert stored is not None and stored["project_id"] == project.id
        output = Path(stored["summary"]["report_path"])
        assert output.parent == Path(roots[project.id]) / ".gobby/reports/dream"
        assert output.name.startswith("gobby-dream-")
        content = output.read_text()
        scope_summary = next(
            item
            for item in result["run"]["summary"]["scope_summaries"]
            if item["scope"] == project.id
        )
        assert scope_summary["report_path"] == str(output)
        assert f"Retain durable conventions for {project.id}." in content
        assert "mutations: 0, noops: 1" in content
        assert len(content.encode()) < 3000
    assert result["run"]["summary"]["noops"] == 3
