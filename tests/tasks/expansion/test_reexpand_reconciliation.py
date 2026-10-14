"""Re-expansion keeps delivered leaves and adds only the new plan sections."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.storage.expansion_runs import ExpansionRun, LocalExpansionRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.expansion_qa_coverage import run_expansion_qa_coverage
from gobby.tasks.expansion_service import ExpansionService
from gobby.utils.machine_id import require_machine_id

pytestmark = pytest.mark.unit

COMMIT = "51151d39d1f7d0a882e08eec2a936052f03f901d"


def _service(temp_db: HubDatabase) -> ExpansionService:
    return ExpansionService(
        task_manager=LocalTaskManager(temp_db),
        llm_service=MagicMock(),
        run_manager=LocalExpansionRunManager(temp_db),
    )


def _save_run(
    service: ExpansionService,
    parent: Task,
    sample_project: dict[str, Any],
    spec: dict[str, Any],
) -> ExpansionRun:
    run = service.run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="plan",
    )
    service.run_manager.start(run.id)
    service.run_manager.save_compiled_spec(run.id, spec)
    return run


def _leaf(stable_id: str, section_id: str, title: str, validation: str) -> dict[str, Any]:
    return {
        "id": stable_id,
        "phase_id": "phase-1",
        "title": title,
        "category": "code",
        "task_type": "feature",
        "validation": validation,
        "description": f"Plan section `{section_id}`.\n\n{title}",
        "labels": [f"covers:plan:{section_id}:{section_id}.1"],
        "source_section_id": section_id,
    }


def _spec(
    leaves: list[dict[str, Any]],
    dependencies: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "contract_plan": True,
        "phases": [
            {
                "id": "phase-1",
                "title": "Phase 1",
                "task_ids": [leaf["id"] for leaf in leaves],
            }
        ],
        "tasks": leaves,
        "dependencies": dependencies or [],
    }


def _parent(service: ExpansionService, sample_project: dict[str, Any]) -> Task:
    return service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Hub epic",
        task_type="epic",
        validation_criteria="Expanded children cover the plan.",
    )


def _session(temp_db: HubDatabase, sample_project: dict[str, Any]) -> Session:
    return SessionManager(temp_db).register(
        external_id="reexpand-seat",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )


def _ids_for(report: dict[str, Any], key: str) -> dict[str, str]:
    rows = report[key]
    assert isinstance(rows, list)
    mapped: dict[str, str] = {}
    for row in rows:
        assert isinstance(row, dict)
        section_id = row["section_id"]
        task_id = row["task_id"]
        assert isinstance(section_id, str)
        assert isinstance(task_id, str)
        mapped[section_id] = task_id
    return mapped


def _task_id(run: Any, stable_id: str) -> str:
    mapping = run.task_id_map
    assert isinstance(mapping, dict)
    task_id = mapping[stable_id]
    assert isinstance(task_id, str)
    return task_id


def _report(run: Any) -> dict[str, Any]:
    checkpoints = run.checkpoints
    assert isinstance(checkpoints, dict)
    report = checkpoints["reconciliation"]
    assert isinstance(report, dict)
    return report


def test_reexpand_preserves_closed_leaf_reuses_open_leaf_and_adds_new_section(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    first = _spec(
        [
            _leaf("leaf-11", "1.1", "Hub-node contract", "Touches src/keep.py."),
            _leaf("leaf-12", "1.2", "Open section", "Touches src/reuse.py."),
        ],
        [{"task_id": "leaf-12", "depends_on": "leaf-11"}],
    )
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    kept_id = _task_id(applied, "leaf-11")
    reused_id = _task_id(applied, "leaf-12")
    service.task_manager.close_task_with_commit(kept_id, COMMIT, reason="completed", force=True)

    amended = _spec(
        [
            _leaf("again-11", "1.1", "Hub-node contract renamed", "Touches src/keep.py."),
            _leaf("again-12", "1.2", "Open section amended", "Touches src/reuse.py."),
            _leaf("new-13", "1.3", "Added section", "Touches src/add.py."),
        ],
        [{"task_id": "new-13", "depends_on": "again-11"}],
    )
    reconciled = service.apply_run(
        _save_run(service, parent, sample_project, amended).id, session_id=None
    )

    report = _report(reconciled)
    assert _ids_for(report, "preserved") == {"1.1": kept_id}
    assert _ids_for(report, "reused") == {"1.2": reused_id}
    assert _ids_for(report, "created") == {"1.3": _task_id(reconciled, "new-13")}
    assert report["removed"] == []
    assert report["untouched"] == []
    kept = service.task_manager.get_task(kept_id)
    assert kept.closed_at is not None
    assert kept.closed_reason == "completed"
    assert kept.title == "Hub-node contract"
    reused = service.task_manager.get_task(reused_id)
    assert reused.closed_at is None
    assert reused.title == "Open section amended"
    created_id = _task_id(reconciled, "new-13")
    assert created_id not in {kept_id, reused_id}
    children = service.task_manager.list_tasks(parent_task_id=parent.id)
    assert sorted(child.title for child in children) == [
        "Added section",
        "Hub-node contract",
        "Open section amended",
    ]
    blockers = {dep.depends_on for dep in service.dep_manager.get_blockers(created_id)}
    assert kept_id in blockers


def test_reexpand_reports_claimed_target_without_reset_or_reassignment(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    session = _session(temp_db, sample_project)
    first = _spec([_leaf("leaf-11", "1.1", "Claimed section", "Touches src/keep.py.")])
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    claimed_id = _task_id(applied, "leaf-11")
    service.task_manager.claim_task(claimed_id, session.id)

    amended = _spec([_leaf("again-11", "1.1", "Stolen title", "Touches src/keep.py.")])
    reconciled = service.apply_run(
        _save_run(service, parent, sample_project, amended).id, session_id=None
    )

    report = _report(reconciled)
    assert report["untouched"] == [
        {"section_id": "1.1", "task_id": claimed_id, "reason": "claimed"}
    ]
    assert report["preserved"] == []
    assert report["reused"] == []
    assert report["created"] == []
    task = service.task_manager.get_task(claimed_id)
    assert task.claimed_by_session_id == session.id
    assert task.title == "Claimed section"
    assert _task_id(reconciled, "again-11") == claimed_id
    assert len(service.task_manager.list_tasks(parent_task_id=parent.id)) == 1


def test_reexpand_reports_in_progress_target_without_changing_it(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    session = _session(temp_db, sample_project)
    service.task_manager.initialize_task_manifest(
        parent.id,
        stage_names=["development", "pr", "merge"],
    )
    first = _spec([_leaf("leaf-11", "1.1", "Started section", "Touches src/keep.py.")])
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    started_id = _task_id(applied, "leaf-11")
    service.task_manager.stage_states.start_stage(
        started_id, "development", by_session_id=session.id
    )

    amended = _spec([_leaf("again-11", "1.1", "Rewritten while active", "Touches src/keep.py.")])
    reconciled = service.apply_run(
        _save_run(service, parent, sample_project, amended).id, session_id=None
    )

    report = _report(reconciled)
    assert report["untouched"] == [
        {"section_id": "1.1", "task_id": started_id, "reason": "in_progress"}
    ]
    task = service.task_manager.get_task(started_id)
    assert task.title == "Started section"
    assert _task_id(reconciled, "again-11") == started_id


def test_reexpand_reports_removed_section_and_keeps_the_task(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    first = _spec(
        [
            _leaf("leaf-11", "1.1", "Kept section", "Touches src/keep.py."),
            _leaf("leaf-12", "1.2", "Removed section", "Touches src/gone.py."),
        ]
    )
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    kept_id = _task_id(applied, "leaf-11")
    removed_id = _task_id(applied, "leaf-12")

    amended = _spec([_leaf("again-11", "1.1", "Kept section amended", "Touches src/keep.py.")])
    reconciled = service.apply_run(
        _save_run(service, parent, sample_project, amended).id, session_id=None
    )

    report = _report(reconciled)
    assert report["removed"] == [{"section_id": "1.2", "task_id": removed_id}]
    removed = service.task_manager.get_task(removed_id)
    assert removed.title == "Removed section"
    assert removed.closed_at is None
    assert _ids_for(report, "reused") == {"1.1": kept_id}
    assert service.task_manager.get_task(removed_id).id == removed_id


def test_reexpand_coverage_counts_preserved_closed_leaf(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(
        """> **Plan ID:** plan

## 1.1 Keep [category: code]
`kind: deliverable`

Keep the delivered section.

**Acceptance:**
- 1.1.1 - Kept. file: `src/keep.py`

## 1.2 Reuse [category: code]
`kind: deliverable`

Reuse the open section.

**Acceptance:**
- 1.2.1 - Reused. file: `src/reuse.py`

## 1.3 Add [category: code]
`kind: deliverable`

Add the new section.

**Acceptance:**
- 1.3.1 - Added. file: `src/add.py`
""",
        encoding="utf-8",
    )
    plan_hash = hashlib.sha256(plan.read_bytes()).hexdigest()
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    first = _spec(
        [
            _leaf("leaf-11", "1.1", "Hub-node contract", "Touches src/keep.py."),
            _leaf("leaf-12", "1.2", "Open section", "Touches src/reuse.py."),
        ]
    )
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    kept_id = _task_id(applied, "leaf-11")
    kept = service.task_manager.close_task_with_commit(
        kept_id, COMMIT, reason="completed", force=True
    )
    amended = _spec(
        [
            _leaf("again-11", "1.1", "Hub-node contract", "Touches src/keep.py."),
            _leaf("again-12", "1.2", "Open section amended", "Touches src/reuse.py."),
            _leaf("new-13", "1.3", "Added section", "Touches src/add.py."),
        ]
    )
    reconciled = service.apply_run(
        _save_run(service, parent, sample_project, amended).id, session_id=None
    )

    result = run_expansion_qa_coverage(
        task_manager=service.task_manager,
        run=reconciled,
        repo_path=tmp_path,
        plan_path="plan.md",
        plan_id="plan",
        plan_hash=plan_hash,
        root_task_ref=str(parent.seq_num),
        project_id=sample_project["id"],
        is_spawned_agent=False,
    )

    assert result["ok"] is True
    assert result["passed"] is True
    qa_result = result["qa_result"]
    assert isinstance(qa_result, dict)
    coverage = qa_result["coverage"]
    assert isinstance(coverage, dict)
    rows = coverage["rows"]
    assert isinstance(rows, list)
    covered = next(
        row
        for row in rows
        if isinstance(row, dict) and row["section_id"] == "1.1" and row["item_id"] == "1.1.1"
    )
    assert covered["status"] == "covered"
    leaves = covered["leaves"]
    assert isinstance(leaves, list)
    first_leaf = leaves[0]
    assert isinstance(first_leaf, dict)
    assert first_leaf["leaf_task_ref"] == f"#{kept.seq_num}"
