"""Task CRUD storage signatures must drop legacy state columns."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.storage.tasks import LocalTaskManager, _crud, _updates

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


class _Cursor:
    rowcount = 1


class _CaptureTransaction:
    def __init__(self, db: _CapturePostgresDb) -> None:
        self.db = db

    def __enter__(self) -> _CaptureTransaction:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Cursor:
        self.db.execute_calls.append((sql, params))
        return _Cursor()


class _CapturePostgresDb:
    dialect = "postgres"

    def __init__(self) -> None:
        self.fetchall_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, str]]:
        self.fetchall_calls.append((sql, params))
        return [{"name": "validation_fail_count"}, {"name": "updated_at"}]

    def transaction(self) -> _CaptureTransaction:
        return _CaptureTransaction(self)


def test_update_task_uses_postgres_column_introspection(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _CapturePostgresDb()
    monkeypatch.setattr(
        _updates,
        "get_task",
        lambda db, task_id: SimpleNamespace(closed_at=None, escalated_at=None),
    )

    changed_parent = _crud.update_task(db, "task-1", validation_fail_count=1)

    assert changed_parent is False
    column_sql, column_params = db.fetchall_calls[0]
    assert "information_schema.columns" in column_sql
    assert column_params == ("tasks",)
    assert db.execute_calls[0][0] == (
        "UPDATE tasks SET validation_fail_count = ?, updated_at = ? WHERE id = ?"
    )
