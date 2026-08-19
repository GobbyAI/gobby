from __future__ import annotations

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
import yaml

from gobby.plans.bootstrap_ledger import (
    BootstrapLedgerMismatchError,
    bootstrap_ledger_path_for_task,
    verify_bootstrap_ledger,
)
from gobby.plans.coverage_manifest import coverage_manifest_path
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.plans import LocalPlanManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task

pytestmark = pytest.mark.unit


def test_close_succeeds_despite_ledger_mismatch(temp_db: HubDatabase, tmp_path: Path) -> None:
    root, leaf, project_id = _seed_plan_task_tree(
        temp_db, tmp_path, expected_leaf_title="Expected leaf"
    )
    repo = tmp_path / "repo"
    _write_plan_row(
        temp_db,
        project_id=project_id,
        root_ref=str(root.seq_num),
        plan_id="task-100-plan",
    )
    _write_ledger(
        repo,
        project_id=project_id,
        root_ref=str(root.seq_num),
        plan_id="task-100-plan",
        title="Expected leaf",
    )
    _write_manifest(
        repo,
        project_id=project_id,
        root_ref=str(root.seq_num),
        plan_id="task-100-plan",
        leaf_ref="#999999",
    )

    with pytest.raises(BootstrapLedgerMismatchError) as exc_info:
        verify_bootstrap_ledger(temp_db, root.id)
    assert "A1:A1.1" in str(exc_info.value)
    assert exc_info.value.to_response()["error"] == "bootstrap_ledger_mismatch"

    manager = LocalTaskManager(temp_db)
    manager.close_task(leaf.id)
    closed_root = manager.get_task(root.id)
    assert closed_root is not None
    assert closed_root.closed_at is not None


def test_close_succeeds_on_ledger_match(temp_db: HubDatabase, tmp_path: Path) -> None:
    root, leaf, project_id = _seed_plan_task_tree(
        temp_db, tmp_path, expected_leaf_title="Expected leaf"
    )
    repo = tmp_path / "repo"
    _write_plan_row(
        temp_db,
        project_id=project_id,
        root_ref=str(root.seq_num),
        plan_id="task-100-plan",
    )
    _write_ledger(
        repo,
        project_id=project_id,
        root_ref=str(root.seq_num),
        plan_id="task-100-plan",
        title=leaf.title,
    )
    _write_manifest(
        repo,
        project_id=project_id,
        root_ref=str(root.seq_num),
        plan_id="task-100-plan",
        leaf_ref=f"#{leaf.seq_num}",
    )

    assert bootstrap_ledger_path_for_task(temp_db, root.id) == (
        repo / ".gobby" / "plans" / "task-100-plan.coverage-ledger.yaml"
    )
    verify_bootstrap_ledger(temp_db, root.id)


@pytest.mark.parametrize("mismatch_field", ["plan_id", "root_task_ref"])
def test_close_succeeds_on_ledger_identity_mismatch(
    temp_db: HubDatabase,
    tmp_path: Path,
    mismatch_field: str,
) -> None:
    root, leaf, project_id = _seed_plan_task_tree(
        temp_db, tmp_path, expected_leaf_title="Expected leaf"
    )
    repo = tmp_path / "repo"
    entry_plan_id = "task-100-plan"
    entry_root_ref = str(root.seq_num)
    ledger_plan_id = "copied-task-plan" if mismatch_field == "plan_id" else entry_plan_id
    ledger_root_ref = "999999" if mismatch_field == "root_task_ref" else entry_root_ref
    _write_plan_row(
        temp_db,
        project_id=project_id,
        root_ref=entry_root_ref,
        plan_id=entry_plan_id,
    )
    _write_ledger(
        repo,
        project_id=project_id,
        root_ref=ledger_root_ref,
        plan_id=ledger_plan_id,
        filename_plan_id=entry_plan_id,
        title=leaf.title,
    )
    _write_manifest(
        repo,
        project_id=project_id,
        root_ref=ledger_root_ref,
        plan_id=ledger_plan_id,
        leaf_ref=f"#{leaf.seq_num}",
    )

    with pytest.raises(BootstrapLedgerMismatchError) as exc_info:
        verify_bootstrap_ledger(temp_db, root.id)
    assert exc_info.value.to_response()["error"] == "bootstrap_ledger_mismatch"

    manager = LocalTaskManager(temp_db)
    manager.close_task(leaf.id)
    closed_root = manager.get_task(root.id)
    assert closed_root is not None
    assert closed_root.closed_at is not None

    assert f"ledger {mismatch_field}" in str(exc_info.value)
    assert "locating plan entry" in str(exc_info.value)


@pytest.mark.parametrize("omitted_field", ["plan_id", "root_task_ref"])
def test_ledger_identity_omission_uses_plan_entry_fallback(
    temp_db: HubDatabase,
    tmp_path: Path,
    omitted_field: str,
) -> None:
    root, leaf, project_id = _seed_plan_task_tree(
        temp_db, tmp_path, expected_leaf_title="Expected leaf"
    )
    repo = tmp_path / "repo"
    entry_plan_id = "task-100-plan"
    entry_root_ref = str(root.seq_num)
    _write_plan_row(
        temp_db,
        project_id=project_id,
        root_ref=entry_root_ref,
        plan_id=entry_plan_id,
    )
    _write_ledger(
        repo,
        project_id=project_id,
        root_ref=None if omitted_field == "root_task_ref" else entry_root_ref,
        plan_id=None if omitted_field == "plan_id" else entry_plan_id,
        filename_plan_id=entry_plan_id,
        title=leaf.title,
    )
    _write_manifest(
        repo,
        project_id=project_id,
        root_ref=entry_root_ref,
        plan_id=entry_plan_id,
        leaf_ref=f"#{leaf.seq_num}",
    )

    assert bootstrap_ledger_path_for_task(temp_db, root.id) == (
        repo / ".gobby" / "plans" / "task-100-plan.coverage-ledger.yaml"
    )
    verify_bootstrap_ledger(temp_db, root.id)


def test_archived_plan_companions_do_not_block_normal_root_close(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    root, leaf, project_id = _seed_plan_task_tree(
        temp_db, tmp_path, expected_leaf_title="Expected leaf"
    )
    repo = tmp_path / "repo"
    plan_id = "task-100-plan"
    _write_plan_row(
        temp_db,
        project_id=project_id,
        root_ref=str(root.seq_num),
        plan_id=plan_id,
    )
    _write_ledger(
        repo,
        project_id=project_id,
        root_ref=str(root.seq_num),
        plan_id=plan_id,
        title=leaf.title,
    )
    manifest_path = _write_manifest(
        repo,
        project_id=project_id,
        root_ref=str(root.seq_num),
        plan_id=plan_id,
        leaf_ref="#999999",
    )
    ledger_path = repo / ".gobby" / "plans" / f"{plan_id}.coverage-ledger.yaml"
    plan_path = repo / ".gobby" / "plans" / f"{plan_id}.md"
    plan_path.write_text("# Archived plan\n", encoding="utf-8")

    archived = LocalPlanManager(temp_db).archive_plan(plan_id, project_id=project_id)

    assert archived.state == "archived"
    assert not manifest_path.exists()
    assert not ledger_path.exists()

    # A stale pre-fix ledger must not reactivate verification for an archived plan.
    _write_ledger(
        repo,
        project_id=project_id,
        root_ref=str(root.seq_num),
        plan_id=plan_id,
        title=leaf.title,
    )
    task_manager = LocalTaskManager(temp_db)
    task_manager.close_task(leaf.id, force=True)

    closed_root = task_manager.close_task(root.id)

    assert closed_root.closed_at is not None


def _seed_plan_task_tree(
    temp_db: HubDatabase, tmp_path: Path, *, expected_leaf_title: str
) -> tuple[Task, Task, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = LocalProjectManager(temp_db).create(
        name="plan-project",
        repo_path=str(repo),
    )
    task_manager = LocalTaskManager(temp_db)
    root = task_manager.create_task(
        project.id,
        title="Root plan task",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project.id,
        title=expected_leaf_title,
        parent_task_id=root.id,
        labels=["covers:task-100-plan:A1:A1.1"],
        validation_criteria="touches src/gobby/plans/bootstrap_ledger.py",
    )
    return root, leaf, project.id


def _write_plan_row(temp_db: HubDatabase, *, project_id: str, root_ref: str, plan_id: str) -> None:
    temp_db.execute(
        """
        INSERT INTO plans (
            id, project_id, plan_id, plan_path, plan_hash, plan_kind, state,
            root_task_ref, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, 'hash-1', 'implementation', 'active', %s,
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """,
        (
            str(uuid5(NAMESPACE_URL, f"gobby-plan:{plan_id}")),
            project_id,
            plan_id,
            f".gobby/plans/{plan_id}.md",
            root_ref,
        ),
    )


def _write_ledger(
    repo: Path,
    *,
    project_id: str,
    root_ref: str | None,
    plan_id: str | None,
    title: str,
    filename_plan_id: str | None = None,
) -> None:
    (repo / ".gobby" / "plans").mkdir(parents=True, exist_ok=True)
    ledger_plan_id = filename_plan_id or plan_id
    if ledger_plan_id is None:
        raise ValueError("ledger filename requires a plan ID")
    ledger: dict[str, object] = {
        "project_id": project_id,
        "plan_hash": "hash-1",
        "sections": {
            "A1": {
                "acceptance_items": {
                    "A1.1": {
                        "expected_leaves": [
                            {
                                "title": title,
                                "owner_agent": "backend-developer",
                                "validation_criteria_summary": "expected",
                            }
                        ]
                    }
                }
            }
        },
    }
    if plan_id is not None:
        ledger["plan_id"] = plan_id
    if root_ref is not None:
        ledger["root_task_ref"] = root_ref
    (repo / ".gobby" / "plans" / f"{ledger_plan_id}.coverage-ledger.yaml").write_text(
        yaml.safe_dump(ledger, sort_keys=False),
        encoding="utf-8",
    )


def _write_manifest(
    repo: Path, *, project_id: str, root_ref: str, plan_id: str, leaf_ref: str
) -> Path:
    manifest = coverage_manifest_path(
        repo,
        project_id=project_id,
        root_task_ref=root_ref,
        plan_id=plan_id,
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        yaml.safe_dump(
            {
                "header": {
                    "plan_id": plan_id,
                    "plan_hash": "hash-1",
                    "root_task_ref": root_ref,
                    "project_id": project_id,
                    "generated_at": "2026-04-27T00:00:00Z",
                },
                "rows": [
                    {
                        "section_id": "A1",
                        "item_id": "A1.1",
                        "status": "covered",
                        "leaves": [
                            {
                                "leaf_task_ref": leaf_ref,
                                "validation_criteria_snippet": "expected",
                                "matched_artifact_ref": "src/gobby/plans/bootstrap_ledger.py",
                            }
                        ],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return manifest
