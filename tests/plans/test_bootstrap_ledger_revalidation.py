from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.plans.bootstrap_ledger import (
    BootstrapLedgerMismatchError,
    bootstrap_ledger_path_for_task,
    verify_bootstrap_ledger,
)
from gobby.plans.coverage_manifest import coverage_manifest_path
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task

pytestmark = pytest.mark.unit


def test_close_blocked_on_ledger_mismatch(temp_db, tmp_path: Path) -> None:
    root, _leaf, project_id = _seed_plan_task_tree(
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


def test_close_succeeds_on_ledger_match(temp_db, tmp_path: Path) -> None:
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


def _seed_plan_task_tree(
    temp_db, tmp_path: Path, *, expected_leaf_title: str
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
    )
    leaf = task_manager.create_task(
        project.id,
        title=expected_leaf_title,
        parent_task_id=root.id,
        labels=["covers:task-100-plan:A1:A1.1"],
        validation_criteria="touches src/gobby/plans/bootstrap_ledger.py",
    )
    return root, leaf, project.id


def _write_plan_row(
    temp_db: HubDatabase, *, project_id: str, root_ref: str, plan_id: str
) -> None:
    temp_db.execute(
        """
        INSERT INTO plans (
            id, project_id, plan_id, plan_path, plan_hash, plan_kind, state,
            root_task_ref, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 'hash-1', 'implementation', 'active', ?,
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """,
        (
            f"plan-{plan_id}",
            project_id,
            plan_id,
            f".gobby/plans/{plan_id}.md",
            root_ref,
        ),
    )


def _write_ledger(repo: Path, *, project_id: str, root_ref: str, plan_id: str, title: str) -> None:
    (repo / ".gobby" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / ".gobby" / "plans" / f"{plan_id}.coverage-ledger.yaml").write_text(
        yaml.safe_dump(
            {
                "project_id": project_id,
                "plan_id": plan_id,
                "plan_hash": "hash-1",
                "root_task_ref": root_ref,
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
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_manifest(
    repo: Path, *, project_id: str, root_ref: str, plan_id: str, leaf_ref: str
) -> None:
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
