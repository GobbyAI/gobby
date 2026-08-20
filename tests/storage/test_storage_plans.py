"""Red tests for coverage manifest lifecycle hooks."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from gobby.plans.coverage_manifest import coverage_manifest_path
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.plans import LocalPlanManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _project(temp_db: HubDatabase, root: Path) -> str:
    return LocalProjectManager(temp_db).create(name="plans-red", repo_path=str(root)).id


def _root_task_ref(temp_db: HubDatabase, project_id: str) -> str:
    task = LocalTaskManager(temp_db).create_task(
        project_id=project_id,
        title="Plan root",
        validation_criteria="Storage fixture task; behavior asserted by the test.",
    )
    return f"#{task.seq_num}"


def _write_plan(root: Path) -> Path:
    path = root / ".gobby" / "plans" / "task-200-red.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            """
            > **Plan ID:** task-200-red

            ## P1 Phase 1
            `kind: framing`

            ### 1.1 Leaf [category: code]
            `kind: deliverable`

            Build it.

            **Acceptance:**
            - 1.1.1 - Leaf exists. file: `src/leaf.py`
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return path


def test_update_plan_hash_regenerates_manifest(temp_db: HubDatabase, tmp_path: Path) -> None:
    manager = LocalPlanManager(temp_db)
    project_id = _project(temp_db, tmp_path)
    root_task_ref = _root_task_ref(temp_db, project_id)
    plan = _write_plan(tmp_path)
    record = manager.create_plan(
        project_id=project_id,
        plan_id="task-200-red",
        plan_path=plan,
        root_task_ref=root_task_ref,
    )
    manifest = coverage_manifest_path(
        tmp_path,
        project_id=project_id,
        root_task_ref=root_task_ref,
        plan_id="task-200-red",
    )
    plan.write_text(plan.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    updated = manager.update_plan_hash(record.plan_id, project_id=project_id)

    assert hasattr(manager, "generate_coverage_manifest")
    assert updated.plan_hash != record.plan_hash
    assert updated.plan_hash in manifest.read_text(encoding="utf-8")


def test_archive_removes_coverage_manifest(temp_db: HubDatabase, tmp_path: Path) -> None:
    manager = LocalPlanManager(temp_db)
    project_id = _project(temp_db, tmp_path)
    root_task_ref = _root_task_ref(temp_db, project_id)
    plan = _write_plan(tmp_path)
    manager.create_plan(
        project_id=project_id,
        plan_id="task-200-red",
        plan_path=plan,
        root_task_ref=root_task_ref,
    )
    manifest = coverage_manifest_path(
        tmp_path,
        project_id=project_id,
        root_task_ref=root_task_ref,
        plan_id="task-200-red",
    )
    ledger = tmp_path / ".gobby" / "plans" / "task-200-red.coverage-ledger.yaml"
    ledger.write_text("header: {}\n", encoding="utf-8")
    assert manifest.exists()
    assert ledger.exists()

    manager.archive_plan("task-200-red", project_id=project_id)

    assert not manifest.exists()
    assert not ledger.exists()


def _write_strategy_plan(root: Path) -> Path:
    path = root / ".gobby" / "plans" / "strategy-300.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            """
            > **Plan ID:** strategy-300

            ## Context

            Freeform narrative headings with no section IDs.

            ## Findings

            More narrative.
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return path


def test_strategy_plan_registers_without_coverage_manifest(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    manager = LocalPlanManager(temp_db)
    project_id = _project(temp_db, tmp_path)
    root_task_ref = _root_task_ref(temp_db, project_id)
    plan = _write_strategy_plan(tmp_path)
    record = manager.create_plan(
        project_id=project_id,
        plan_id="strategy-300",
        plan_path=plan,
        plan_kind="strategy",
        root_task_ref=root_task_ref,
    )
    manifest = coverage_manifest_path(
        tmp_path,
        project_id=project_id,
        root_task_ref=root_task_ref,
        plan_id="strategy-300",
    )

    assert record.plan_kind == "strategy"
    assert not manifest.exists()

    plan.write_text(plan.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    updated = manager.update_plan_hash(record.plan_id, project_id=project_id)

    assert updated.plan_hash != record.plan_hash
    assert not manifest.exists()

    with pytest.raises(ValueError, match="does not carry a coverage manifest"):
        manager.regenerate_coverage_manifest("strategy-300", project_id=project_id)
