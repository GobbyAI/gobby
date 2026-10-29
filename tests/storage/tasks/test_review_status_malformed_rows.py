"""A malformed review step row reads as no review, and step writers refuse one."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.task_review_serialization import deserialize_task_step_records
from gobby.tasks.step_records import StepSubmission, TaskStepRecord

pytestmark = pytest.mark.unit

_SHA = "a" * 40
_REVIEW_ROW: dict[str, Any] = {
    "step": "review",
    "status": "approved",
    "epoch": 1,
    "round_number": 1,
    "commit_sha": None,
    "sequence": 1,
    "agent_definition": "reviewer",
    "supplemental_review": False,
    "session_id": "reviewer-session",
}


@pytest.mark.parametrize("field", ["epoch", "sequence"])
def test_review_status_read_counts_a_malformed_review_row_as_no_review(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    field: str,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Malformed review row",
        validation_criteria="Review status reads without raising.",
    )
    body = {**_REVIEW_ROW, field: "not-an-integer"}
    temp_db.execute(
        "INSERT INTO task_comments (id, task_id, author, author_type, body) "
        "VALUES (%s, %s, %s, 'task_step', %s)",
        (str(uuid4()), task.id, "reviewer-session", json.dumps(body)),
    )

    assert manager.get_task(task.id).review_status == "pending"


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"supplemental_review": None},
        {"supplemental_review": "false"},
        {"supplemental_review": True},
        {"supplemental_review": False},
    ],
    ids=["absent", "null", "string", "supplemental", "primary"],
)
@pytest.mark.parametrize("history", ["open", "closed", "older_primary"])
def test_review_status_requires_explicit_primary_authority(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    snapshot: dict[str, object],
    history: str,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Restored review authority",
        validation_criteria="Only explicit primary authority projects an open task as valid.",
    )
    temp_db.execute(
        "UPDATE tasks SET review_status = 'valid', closed_at = %s, closed_reason = %s WHERE id = %s",
        (
            datetime.now(UTC) if history == "closed" else None,
            "done" if history == "closed" else None,
            task.id,
        ),
    )
    if history == "older_primary":
        temp_db.execute(
            "INSERT INTO task_comments (id, task_id, author, author_type, body) "
            "VALUES (%s, %s, %s, 'task_step', %s)",
            (str(uuid4()), task.id, "reviewer-session", json.dumps(_REVIEW_ROW)),
        )
    body = {key: value for key, value in _REVIEW_ROW.items() if key != "supplemental_review"}
    body.update({"sequence": 2, **snapshot})
    temp_db.execute(
        "INSERT INTO task_comments (id, task_id, author, author_type, body) "
        "VALUES (%s, %s, %s, 'task_step', %s)",
        (str(uuid4()), task.id, "reviewer-session", json.dumps(body)),
    )

    expected = (
        "pending"
        if history == "open" and snapshot.get("supplemental_review") is not False
        else "valid"
    )
    assert manager.get_task(task.id).review_status == expected


@pytest.mark.parametrize("field", ["epoch", "round_number"])
def test_step_submission_refuses_a_non_integer_epoch_or_round(field: str) -> None:
    submission = {
        "status": "approved",
        "epoch": 1,
        "round_number": 1,
        "feedback": "Reviewed at the tip.",
        "commit_sha": _SHA,
        field: "1",
    }

    with pytest.raises(ValidationError):
        StepSubmission.model_validate(submission)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("epoch", "not-an-integer"),
        ("sequence", "not-an-integer"),
        ("sequence", "1"),
        ("sequence", 1.0),
        ("sequence", True),
    ],
)
def test_backed_up_step_history_refuses_a_non_integer_field(field: str, value: object) -> None:
    task_id = str(uuid4())
    now = datetime.now(UTC)
    record = TaskStepRecord(
        status="approved",
        epoch=1,
        round_number=1,
        feedback="Reviewed at the tip.",
        commit_sha=_SHA,
        id=str(uuid4()),
        task_id=task_id,
        step="review",
        sequence=1,
        agent_definition="reviewer",
        agent_definition_version="1",
        agent_definition_hash="hash-1",
        supplemental_review=False,
        session_id=str(uuid4()),
        created_at=now,
        updated_at=now,
    )
    entry = {**record.model_dump(mode="json"), field: value}

    with pytest.raises(ValueError):
        deserialize_task_step_records({"epoch": 1, "step_records": [entry]}, task_id=task_id)
