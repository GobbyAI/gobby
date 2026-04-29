from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
import yaml

from gobby.storage.projects import LocalProjectManager
from scripts.migrate_index_to_plans_table import migrate

pytestmark = pytest.mark.unit


def test_migrates_active_and_completed_plans_then_deletes_index(temp_db, tmp_path: Path) -> None:
    project_id = LocalProjectManager(temp_db).create(name="plans", repo_path=str(tmp_path)).id
    _write_project(tmp_path, project_id)
    active = _write_plan(tmp_path / ".gobby" / "plans", "task-100-active")
    completed = _write_plan(tmp_path / ".gobby" / "plans" / "completed", "task-101-done")
    index = tmp_path / ".gobby" / "plans" / ("index" + ".yaml")
    index.write_text(
        yaml.safe_dump(
            {
                "entries": [
                    {
                        "plan_id": active.stem,
                        "project_id": project_id,
                        "root_task_ref": "100",
                        "plan_kind": "implementation",
                    },
                    {
                        "plan_id": completed.stem,
                        "project_id": project_id,
                        "root_task_ref": "101",
                        "plan_kind": "implementation",
                    },
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    assert migrate(tmp_path, temp_db) == 2
    assert not index.exists()
    assert migrate(tmp_path, temp_db) == 2

    rows = temp_db.fetchall("SELECT plan_id, root_task_ref, state FROM plans ORDER BY plan_id")
    assert [(row["plan_id"], row["root_task_ref"], row["state"]) for row in rows] == [
        ("task-100-active", "100", "active"),
        ("task-101-done", "101", "archived"),
    ]


def test_keep_index_preserves_source_file(temp_db, tmp_path: Path) -> None:
    project_id = LocalProjectManager(temp_db).create(name="plans", repo_path=str(tmp_path)).id
    _write_project(tmp_path, project_id)
    _write_plan(tmp_path / ".gobby" / "plans", "task-100-active")
    index = tmp_path / ".gobby" / "plans" / ("index" + ".yaml")
    index.write_text("entries: []\n", encoding="utf-8")

    migrate(tmp_path, temp_db, delete_index=False)

    assert index.exists()


def _write_project(root: Path, project_id: str) -> None:
    project_dir = root / ".gobby"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "project.json").write_text(
        json.dumps({"id": project_id}),
        encoding="utf-8",
    )


def _write_plan(directory: Path, plan_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{plan_id}.md"
    path.write_text(
        textwrap.dedent(
            f"""
            > **Plan ID:** {plan_id}

            ## P1 Phase
            `kind: framing`

            ### 1.1 Work [category: code]
            `kind: deliverable`

            Build it.

            **Acceptance:**
            - 1.1.1 - Work exists. file: `src/work.py`
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return path
