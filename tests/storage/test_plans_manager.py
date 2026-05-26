"""Tests for DB-backed plan manager."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from gobby.plans.coverage_manifest import coverage_manifest_path
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.plans import LocalPlanManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _write_plan(root: Path, name: str = "task-100-demo.md") -> Path:
    plan_dir = root / ".gobby" / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            """
            > **Plan ID:** task-100-demo

            ## P1 Phase 1
            `kind: framing`

            ### 1.1 Foundation [category: code]
            `kind: deliverable`

            Build it.

            **Acceptance:**
            - 1.1.1 — Foundation exists. file: `src/foundation.py`
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return path


def _project(temp_db: HubDatabase, root: Path) -> str:
    return LocalProjectManager(temp_db).create(name="plans", repo_path=str(root)).id


def test_create_plan_emits_initial_manifest(temp_db: HubDatabase, tmp_path: Path) -> None:
    project_id = _project(temp_db, tmp_path)
    LocalTaskManager(temp_db).create_task(project_id=project_id, title="Root")
    plan_path = _write_plan(tmp_path)

    record = LocalPlanManager(temp_db).create_plan(
        project_id=project_id,
        plan_id="task-100-demo",
        plan_path=plan_path,
        root_task_ref="#100",
    )

    assert record.state == "active"
    assert record.plan_path == ".gobby/plans/task-100-demo.md"
    manifest = coverage_manifest_path(
        tmp_path,
        project_id=project_id,
        root_task_ref="#100",
        plan_id="task-100-demo",
    )
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert raw["header"]["plan_hash"] == record.plan_hash
    assert raw["rows"][0]["status"] == "missing"


def test_update_plan_hash_regens_manifest(temp_db: HubDatabase, tmp_path: Path) -> None:
    project_id = _project(temp_db, tmp_path)
    plan_path = _write_plan(tmp_path)
    manager = LocalPlanManager(temp_db)
    first = manager.create_plan(
        project_id=project_id,
        plan_id="task-100-demo",
        plan_path=plan_path,
        root_task_ref="#100",
    )
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    updated = manager.update_plan_hash("task-100-demo", project_id=project_id)

    assert updated.plan_hash != first.plan_hash
    manifest = coverage_manifest_path(
        tmp_path,
        project_id=project_id,
        root_task_ref="#100",
        plan_id="task-100-demo",
    )
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert raw["header"]["plan_hash"] == updated.plan_hash


def test_archive_plan_moves_file_and_removes_manifest(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    project_id = _project(temp_db, tmp_path)
    plan_path = _write_plan(tmp_path)
    manager = LocalPlanManager(temp_db)
    manager.create_plan(
        project_id=project_id,
        plan_id="task-100-demo",
        plan_path=plan_path,
        root_task_ref="#100",
    )
    manifest = coverage_manifest_path(
        tmp_path,
        project_id=project_id,
        root_task_ref="#100",
        plan_id="task-100-demo",
    )

    archived = manager.archive_plan("task-100-demo", project_id=project_id)

    assert archived.state == "archived"
    assert archived.archived_at is not None
    assert archived.plan_path == ".gobby/plans/completed/task-100-demo.md"
    assert not plan_path.exists()
    assert (tmp_path / archived.plan_path).exists()
    assert not manifest.exists()
    assert manager.archive_plan("task-100-demo", project_id=project_id) == archived


def test_archive_plan_preserves_nested_relative_paths(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    project_id = _project(temp_db, tmp_path)
    alpha_path = _write_plan(tmp_path, "alpha/task.md")
    beta_path = _write_plan(tmp_path, "beta/task.md")
    manager = LocalPlanManager(temp_db)
    manager.create_plan(
        project_id=project_id,
        plan_id="task-alpha",
        plan_path=alpha_path,
        root_task_ref="#100",
    )
    manager.create_plan(
        project_id=project_id,
        plan_id="task-beta",
        plan_path=beta_path,
        root_task_ref="#101",
    )

    alpha = manager.archive_plan("task-alpha", project_id=project_id)
    beta = manager.archive_plan("task-beta", project_id=project_id)

    assert alpha.plan_path == ".gobby/plans/completed/alpha/task.md"
    assert beta.plan_path == ".gobby/plans/completed/beta/task.md"
    assert (tmp_path / alpha.plan_path).exists()
    assert (tmp_path / beta.plan_path).exists()
