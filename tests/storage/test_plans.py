"""Red tests for coverage manifest lifecycle hooks."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from gobby.plans.coverage_manifest import coverage_manifest_path
from gobby.storage.plans import LocalPlanManager
from gobby.storage.projects import LocalProjectManager

pytestmark = pytest.mark.unit


def _project(temp_db, root: Path) -> str:
    return LocalProjectManager(temp_db).create(name="plans-red", repo_path=str(root)).id


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


def test_update_plan_hash_regenerates_manifest(temp_db, tmp_path: Path) -> None:
    manager = LocalPlanManager(temp_db)
    project_id = _project(temp_db, tmp_path)
    plan = _write_plan(tmp_path)
    record = manager.create_plan(
        project_id=project_id,
        plan_id="task-200-red",
        plan_path=plan,
        root_task_ref="#200",
    )
    manifest = coverage_manifest_path(
        tmp_path,
        project_id=project_id,
        root_task_ref="#200",
        plan_id="task-200-red",
    )
    first_mtime = manifest.stat().st_mtime_ns
    plan.write_text(plan.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    updated = manager.update_plan_hash(record.plan_id, project_id=project_id)

    assert hasattr(manager, "_generate_coverage_manifest")
    assert updated.plan_hash != record.plan_hash
    assert manifest.stat().st_mtime_ns > first_mtime


def test_archive_removes_coverage_manifest(temp_db, tmp_path: Path) -> None:
    manager = LocalPlanManager(temp_db)
    project_id = _project(temp_db, tmp_path)
    plan = _write_plan(tmp_path)
    manager.create_plan(
        project_id=project_id,
        plan_id="task-200-red",
        plan_path=plan,
        root_task_ref="#200",
    )
    manifest = coverage_manifest_path(
        tmp_path,
        project_id=project_id,
        root_task_ref="#200",
        plan_id="task-200-red",
    )
    assert manifest.exists()

    manager.archive_plan("task-200-red", project_id=project_id)

    assert not manifest.exists()
