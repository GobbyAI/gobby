"""Closed reviews remain valid at the closure commit across hydrated reads."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._read import find_task_by_prefix, find_tasks_by_prefix

pytestmark = pytest.mark.integration

_APPROVED = "a" * 40
_OTHER = "b" * 40


@pytest.mark.parametrize("reader", ["get", "list", "find", "find_many"])
@pytest.mark.parametrize(
    ("closed_sha", "commits", "expected"),
    [
        pytest.param(_APPROVED, [_APPROVED[:10], _OTHER[:10]], "valid", id="closed-non-tip"),
        pytest.param(_APPROVED, [_OTHER[:10], _APPROVED[:10]], "valid", id="closed-tip"),
        pytest.param(None, [_APPROVED[:10], _OTHER[:10]], "pending", id="open-stale"),
        pytest.param(_OTHER, [_OTHER[:10], _APPROVED[:10]], "pending", id="wrong-closure"),
    ],
)
def test_review_status_uses_closed_candidate_only_after_closure(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    reader: str,
    closed_sha: str | None,
    commits: list[str],
    expected: str,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Review at the closure candidate",
        validation_criteria="Closed reviews survive later links; open reviews become stale.",
    )
    for commit in commits:
        manager.link_commit(task.id, commit)
    if closed_sha is not None:
        manager.close_task(task.id, reason="done", closed_commit_sha=closed_sha)
    # A stored valid value must not hide a stale or mismatched review record.
    temp_db.execute("UPDATE tasks SET review_status = 'valid' WHERE id = %s", (task.id,))
    review = {
        "step": "review",
        "status": "approved",
        "epoch": task.review_epoch,
        "round_number": 1,
        "commit_sha": _APPROVED,
        "sequence": 1,
        "agent_definition": "code-reviewer",
        "session_id": "reviewer-session",
    }
    temp_db.execute(
        "INSERT INTO task_comments (id, task_id, author, author_type, body) "
        "VALUES (%s, %s, %s, 'task_step', %s)",
        (str(uuid4()), task.id, "reviewer-session", json.dumps(review)),
    )

    if reader == "get":
        actual = manager.get_task(task.id)
    elif reader == "list":
        actual = next(
            t for t in manager.list_tasks(project_id=sample_project["id"]) if t.id == task.id
        )
    elif reader == "find":
        actual = find_task_by_prefix(temp_db, task.id)
        assert actual is not None
    else:
        actual = next(t for t in find_tasks_by_prefix(temp_db, task.id) if t.id == task.id)

    assert actual.review_status == expected
