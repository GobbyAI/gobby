"""Task assignment metadata coverage."""

from __future__ import annotations

from dataclasses import MISSING, fields
from datetime import UTC, datetime

import pytest

from gobby.storage.tasks import Task
from gobby.tasks.state_semantics import serialize_task_state

pytestmark = pytest.mark.unit

NEW_TASK_FIELDS = {
    "assigned_agent",
    "implementation_domain",
    "additional_skills",
}


def test_task_dataclass_defines_dispatch_fields_with_safe_defaults() -> None:
    task_fields = {item.name: item for item in fields(Task)}

    assert NEW_TASK_FIELDS.issubset(task_fields)
    assert task_fields["assigned_agent"].default is None
    assert task_fields["implementation_domain"].default is None
    assert task_fields["additional_skills"].default is None
    for field_name in NEW_TASK_FIELDS:
        assert task_fields[field_name].default_factory is MISSING


@pytest.mark.parametrize("serializer", ["serialize_task_state", "to_dict", "to_brief"])
def test_task_serializers_surface_dispatch_fields(serializer: str) -> None:
    task = Task(
        id="task-1",
        project_id="project-1",
        title="Assigned epic",
        priority=1,
        task_type="epic",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        assigned_agent="backend-developer",
        implementation_domain="backend",
        additional_skills=["sql-review", "perf-review"],
    )

    if serializer == "serialize_task_state":
        payload = serialize_task_state(task)
    else:
        payload = getattr(task, serializer)()

    assert payload["assigned_agent"] == "backend-developer"
    assert payload["implementation_domain"] == "backend"
    assert payload["additional_skills"] == ["sql-review", "perf-review"]
