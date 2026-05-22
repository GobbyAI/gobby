"""Dispatch metadata coverage for stage-native tasks."""

from __future__ import annotations

from dataclasses import MISSING, fields
from enum import StrEnum
from typing import Any

import pytest

from gobby.tasks.state_semantics import serialize_task_state

pytestmark = pytest.mark.unit

EXPECTED_ISOLATIONS = ["none", "worktree", "clone"]
NEW_TASK_FIELDS = {
    "allow_automation",
    "unattended",
    "isolation",
    "assigned_agent",
    "implementation_domain",
    "additional_skills",
}


def _task_symbols() -> tuple[type[Any], type[StrEnum]]:
    import gobby.storage.tasks as task_module

    return task_module.Task, task_module.Isolation


def test_isolation_is_str_enum_with_dispatch_values() -> None:
    _Task, Isolation = _task_symbols()

    assert issubclass(Isolation, StrEnum)
    assert [item.value for item in Isolation] == EXPECTED_ISOLATIONS


def test_task_dataclass_defines_dispatch_fields_with_safe_defaults() -> None:
    Task, Isolation = _task_symbols()

    task_fields = {item.name: item for item in fields(Task)}

    assert NEW_TASK_FIELDS.issubset(task_fields)
    assert task_fields["allow_automation"].default is False
    assert task_fields["unattended"].default is False
    assert task_fields["isolation"].default is Isolation.worktree
    assert task_fields["assigned_agent"].default is None
    assert task_fields["implementation_domain"].default is None
    assert task_fields["additional_skills"].default is None
    for field_name in NEW_TASK_FIELDS:
        assert task_fields[field_name].default_factory is MISSING


@pytest.mark.parametrize("serializer", ["serialize_task_state", "to_dict", "to_brief"])
def test_task_serializers_surface_dispatch_fields(serializer: str) -> None:
    Task, Isolation = _task_symbols()
    task = Task(
        id="task-1",
        project_id="project-1",
        title="Automated epic",
        priority=1,
        task_type="epic",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        allow_automation=True,
        unattended=True,
        isolation=Isolation.clone,
        assigned_agent="backend-developer",
        implementation_domain="backend",
        additional_skills=["sql-review", "perf-review"],
    )

    if serializer == "serialize_task_state":
        payload = serialize_task_state(task)
    else:
        payload = getattr(task, serializer)()

    assert payload["allow_automation"] is True
    assert payload["unattended"] is True
    assert payload["isolation"] == Isolation.clone
    assert payload["assigned_agent"] == "backend-developer"
    assert payload["implementation_domain"] == "backend"
    assert payload["additional_skills"] == ["sql-review", "perf-review"]
