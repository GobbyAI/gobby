"""Task CRUD storage signatures must drop legacy state columns."""

from __future__ import annotations

import inspect

import pytest

from gobby.storage.tasks import LocalTaskManager, _crud

pytestmark = pytest.mark.unit


def test_create_task_no_status_param() -> None:
    signature = inspect.signature(_crud.create_task)

    assert "status" not in signature.parameters
    assert "lifecycle" not in signature.parameters
    assert "lifecycle_stage" not in signature.parameters


def test_update_task_no_lifecycle_param() -> None:
    signature = inspect.signature(LocalTaskManager.update_task)

    assert "status" not in signature.parameters
    assert "lifecycle" not in signature.parameters
    assert "lifecycle_stage" not in signature.parameters
