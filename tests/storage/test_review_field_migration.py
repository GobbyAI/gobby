"""The live task contract uses the independent-review fields exclusively."""

from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.task_review_serialization import serialize_task_review

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("status", ["pending", "valid", "invalid", "error"])
def test_review_contract_has_no_legacy_fields(
    temp_db: HubDatabase, sample_project: dict[str, Any], status: str
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Review contract", validation_criteria="All review fields round trip."
    )
    assert task.review_status == "pending"
    assert task.review_epoch == 1
    updated = manager.update_task(
        task.id, review_status=status, review_feedback="Full reviewer feedback", review_fail_count=2
    )
    persisted = manager.get_task(task.id)
    assert persisted == updated
    full = persisted.to_dict()
    assert full["review_status"] == status
    assert full["review_feedback"] == "Full reviewer feedback"
    assert full["review_fail_count"] == 2
    for shape in (full, persisted.to_brief()):
        assert shape["review_epoch"] == 1
        assert (
            not {"validation_status", "validation_feedback", "validation_fail_count"} & shape.keys()
        )
    backup = serialize_task_review(persisted)
    assert backup["status"] == status
    assert backup["feedback"] == "Full reviewer feedback"
    assert backup["fail_count"] == 2
    assert backup["epoch"] == 1
    assert "state" not in backup


@pytest.mark.parametrize(
    "field", ["validation_status", "validation_feedback", "validation_fail_count"]
)
def test_retired_review_fields_are_rejected_without_mutation(
    temp_db: HubDatabase, sample_project: dict[str, Any], field: str
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Reject old fields",
        validation_criteria="Retired names cannot mutate tasks.",
    )
    retired_input: dict[str, Any] = {field: "invalid"}
    with pytest.raises(ValueError, match=f"unsupported fields: {field}"):
        manager.update_task(task.id, **retired_input)
    assert manager.get_task(task.id) == task
