"""Automatic close reviews are retired; their records remain readable history."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from uuid import uuid4

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_tool import register_close_task
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import require_machine_id


def test_no_close_reviewer_runtime(temp_db: HubDatabase) -> None:
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, RegistryContext(task_manager=LocalTaskManager(temp_db)))
    assert registry.get_private_callback("promote_close_reviews") is None
    assert "submit_close_review" not in {tool["name"] for tool in registry.list_tools()}


def test_old_reviews_are_history_only(temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
    from gobby.storage.review_history import ReviewHistory

    context = RegistryContext(task_manager=LocalTaskManager(temp_db))
    task = context.task_manager.create_task(
        sample_project["id"], "Historical review", validation_criteria="Reviewer judgment."
    )
    session = context.session_manager.register(
        external_id="historical-close",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    review_id = str(uuid4())
    with temp_db.transaction() as conn:
        conn.execute(
            """INSERT INTO task_close_review_history
               (id, task_id, task_ref, caller_session_id, close_arguments,
                review_fingerprint, evidence_fingerprint, status)
               VALUES (%s, %s, %s, %s, '{}'::jsonb, 'old-review', 'old-evidence', 'queued')""",
            (review_id, task.id, f"#{task.seq_num}", session.id),
        )
    history = ReviewHistory(temp_db)
    record = history.get(review_id)
    assert record is not None and record.status == "queued"
    assert history.for_task(task.id) == [record]
    assert not hasattr(history, "claim_queued")
    assert not hasattr(history, "finish")
    assert history.get(review_id) == record


def test_retired_review_imports_are_absent() -> None:
    retired = {
        "agentic_close_review",
        "close_review_delivery",
        "close_verdict",
        "task_close_reviews",
        "_lifecycle_close_orchestration",
        "_lifecycle_review_gate",
        "_lifecycle_validation",
    }
    root = Path(__file__).resolve().parents[2] / "src/gobby"
    imports: list[str] = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.ImportFrom, ast.Import)):
                continue
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            for name in names:
                if retired.intersection(name.split(".")):
                    imports.append(f"{path.relative_to(root)}:{node.lineno} {name}")
    assert imports == []
