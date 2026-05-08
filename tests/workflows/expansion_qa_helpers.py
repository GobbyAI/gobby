from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import LocalTaskManager, TaskArtifactManager


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_expansion_qa_case(temp_db: Any, project_manager: Any, repo_path: Path) -> dict[str, Any]:
    project = project_manager.create(name="qa-project", repo_path=str(repo_path))
    task_manager = LocalTaskManager(temp_db)
    registry = create_task_ops_registry(task_manager, sync_manager=MagicMock())
    parent = task_manager.create_task(project_id=project.id, title="Expansion parent")
    plan_rel = Path(".gobby/plans/task-qa.md")
    plan_path = repo_path / plan_rel
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "## A1\n"
        "`kind: deliverable`\n\n"
        "**Acceptance:**\n\n"
        "- A1.1 - Build the thing. file: `src/example.py`.\n",
        encoding="utf-8",
    )
    plan_hash = sha256_file(plan_path)
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        parent.id,
        plan_file_path=str(plan_rel),
        plan_file_hash=plan_hash,
    )
    run = LocalExpansionRunManager(temp_db).create(
        parent_task_id=parent.id,
        project_id=project.id,
        triggering_session_id=None,
        input_source="plan",
        plan_file=str(plan_rel),
    )
    return {
        "project": project,
        "task_manager": task_manager,
        "registry": registry,
        "parent": parent,
        "root_task": f"#{parent.seq_num}",
        "plan_rel": plan_rel,
        "plan_path": plan_path,
        "plan_hash": plan_hash,
        "run": run,
    }


def call_args(case: dict[str, Any], *, plan_hash: str | None = None) -> dict[str, Any]:
    return {
        "run_id": case["run"].id,
        "plan_path": str(case["plan_rel"]),
        "plan_id": "task-qa-plan",
        "plan_hash": plan_hash or case["plan_hash"],
        "root_task": case["root_task"],
        "project_id": case["project"].id,
        "task_tree": "db",
    }


def covered_report() -> dict[str, Any]:
    return {
        "header": {"plan_id": "task-qa-plan"},
        "rows": [{"section_id": "A1", "item_id": "A1.1", "status": "covered", "leaves": []}],
    }
