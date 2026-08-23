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
from gobby.storage.tasks import LocalTaskManager, TaskNotFoundError

pytestmark = pytest.mark.unit

VALIDATION_CRITERIA = "Storage fixture task; behavior asserted by the test."


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
    project_id = LocalProjectManager(temp_db).create(name="plans", repo_path=str(root)).id
    tasks = LocalTaskManager(temp_db)
    first = tasks.create_task(
        project_id=project_id, title="Plan root 100", validation_criteria=VALIDATION_CRITERIA
    )
    second = tasks.create_task(
        project_id=project_id, title="Plan root 101", validation_criteria=VALIDATION_CRITERIA
    )
    temp_db.execute("UPDATE tasks SET seq_num = 100 WHERE id = %s", (first.id,))
    temp_db.execute("UPDATE tasks SET seq_num = 101 WHERE id = %s", (second.id,))
    return project_id


def test_create_plan_rejects_missing_root_task(temp_db: HubDatabase, tmp_path: Path) -> None:
    project_id = _project(temp_db, tmp_path)
    plan_path = _write_plan(tmp_path)

    with pytest.raises(TaskNotFoundError, match="Task #999 not found"):
        LocalPlanManager(temp_db).create_plan(
            project_id=project_id,
            plan_id="missing-root",
            plan_path=plan_path,
            root_task_ref="#999",
        )


def test_create_plan_persists_row_without_partial_manifest_when_evaluation_fails(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _project(temp_db, tmp_path)
    manager = LocalPlanManager(temp_db)
    plan_path = _write_plan(tmp_path)

    def fail_manifest(_record: object) -> None:
        raise OSError("manifest write failed")

    monkeypatch.setattr(manager, "generate_coverage_manifest", fail_manifest)

    with pytest.raises(OSError, match="manifest write failed"):
        manager.create_plan(
            project_id=project_id,
            plan_id="manifest-failure",
            plan_path=plan_path,
            root_task_ref="#100",
        )

    record = manager.get_plan("manifest-failure", project_id=project_id)
    manifest = coverage_manifest_path(
        tmp_path,
        project_id=project_id,
        root_task_ref="#100",
        plan_id="manifest-failure",
    )
    assert record.state == "active"
    assert not manifest.exists()


def test_create_plan_emits_initial_manifest(temp_db: HubDatabase, tmp_path: Path) -> None:
    project_id = _project(temp_db, tmp_path)
    LocalTaskManager(temp_db).create_task(
        project_id=project_id, title="Root", validation_criteria=VALIDATION_CRITERIA
    )
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


def test_create_plan_requires_explicit_reactivation(temp_db: HubDatabase, tmp_path: Path) -> None:
    project_id = _project(temp_db, tmp_path)
    manager = LocalPlanManager(temp_db)
    plan_path = _write_plan(tmp_path)
    manager.create_plan(
        project_id=project_id,
        plan_id="task-100-demo",
        plan_path=plan_path,
        root_task_ref="#100",
    )
    manager.archive_plan("task-100-demo", project_id=project_id)
    replacement_path = _write_plan(tmp_path)

    with pytest.raises(ValueError, match="pass reactivate=True"):
        manager.create_plan(
            project_id=project_id,
            plan_id="task-100-demo",
            plan_path=replacement_path,
            root_task_ref="#100",
        )

    archived = manager.get_plan("task-100-demo", project_id=project_id)
    assert archived.state == "archived"
    reactivated = manager.create_plan(
        project_id=project_id,
        plan_id="task-100-demo",
        plan_path=replacement_path,
        root_task_ref="#100",
        reactivate=True,
    )
    assert reactivated.state == "active"
    assert reactivated.archived_at is None


def test_create_plan_refuses_root_task_reassignment(temp_db: HubDatabase, tmp_path: Path) -> None:
    project_id = _project(temp_db, tmp_path)
    manager = LocalPlanManager(temp_db)
    plan_path = _write_plan(tmp_path)
    manager.create_plan(
        project_id=project_id,
        plan_id="task-100-demo",
        plan_path=plan_path,
        root_task_ref="#100",
    )

    with pytest.raises(ValueError, match="already registered to root task 100"):
        manager.create_plan(
            project_id=project_id,
            plan_id="task-100-demo",
            plan_path=plan_path,
            root_task_ref="#101",
        )

    assert manager.get_plan("task-100-demo", project_id=project_id).root_task_ref == "100"


def test_create_plan_stores_root_task_ref_unprefixed(temp_db: HubDatabase, tmp_path: Path) -> None:
    """Expansion QA resolves the manifest by the unprefixed ref, so writes canonicalize."""
    project_id = _project(temp_db, tmp_path)
    manager = LocalPlanManager(temp_db)
    plan_path = _write_plan(tmp_path)

    record = manager.create_plan(
        project_id=project_id,
        plan_id="task-100-demo",
        plan_path=plan_path,
        root_task_ref="#100",
    )

    assert record.root_task_ref == "100"
    manifest = coverage_manifest_path(
        tmp_path,
        project_id=project_id,
        root_task_ref="100",
        plan_id="task-100-demo",
    )
    header = yaml.safe_load(manifest.read_text(encoding="utf-8"))["header"]
    assert header["root_task_ref"] == "100"


def test_create_plan_renormalizes_legacy_prefixed_row(temp_db: HubDatabase, tmp_path: Path) -> None:
    """A row stored before canonicalization re-registers in place instead of conflicting."""
    project_id = _project(temp_db, tmp_path)
    manager = LocalPlanManager(temp_db)
    plan_path = _write_plan(tmp_path)
    record = manager.create_plan(
        project_id=project_id,
        plan_id="task-100-demo",
        plan_path=plan_path,
        root_task_ref="#100",
    )
    with temp_db.transaction() as conn:
        conn.execute(
            "UPDATE plans SET root_task_ref = %s WHERE id = %s",
            ("#100", record.id),
        )

    assert manager.get_plan("task-100-demo", project_id=project_id).root_task_ref == "#100"

    renormalized = manager.create_plan(
        project_id=project_id,
        plan_id="task-100-demo",
        plan_path=plan_path,
        root_task_ref="100",
    )

    assert renormalized.root_task_ref == "100"


def test_get_plan_resolves_root_task_ref_under_either_spelling(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    project_id = _project(temp_db, tmp_path)
    manager = LocalPlanManager(temp_db)
    plan_path = _write_plan(tmp_path)
    manager.create_plan(
        project_id=project_id,
        plan_id="task-100-demo",
        plan_path=plan_path,
        root_task_ref="#100",
    )

    for ref in ("100", "#100"):
        assert manager.get_plan(ref, project_id=project_id).plan_id == "task-100-demo"


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


def test_archive_plan_moves_file_and_removes_manifest(temp_db: HubDatabase, tmp_path: Path) -> None:
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


def test_archive_plan_refuses_existing_destination(temp_db: HubDatabase, tmp_path: Path) -> None:
    project_id = _project(temp_db, tmp_path)
    plan_path = _write_plan(tmp_path)
    manager = LocalPlanManager(temp_db)
    manager.create_plan(
        project_id=project_id,
        plan_id="task-100-demo",
        plan_path=plan_path,
        root_task_ref="#100",
    )
    completed_path = tmp_path / ".gobby" / "plans" / "completed" / plan_path.name
    completed_path.parent.mkdir(parents=True)
    completed_path.write_text("existing archive\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="archive destination already exists"):
        manager.archive_plan("task-100-demo", project_id=project_id)

    assert plan_path.exists()
    assert completed_path.read_text(encoding="utf-8") == "existing archive\n"
    assert manager.get_plan("task-100-demo", project_id=project_id).state == "active"


def test_archive_plan_preserves_nested_relative_paths(temp_db: HubDatabase, tmp_path: Path) -> None:
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
