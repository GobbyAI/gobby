"""Task JSONL import ignores legacy task state keys."""

from __future__ import annotations

import inspect

import pytest

from gobby.sync.tasks import TaskSyncManager
from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def test_import_does_not_write_legacy_columns() -> None:
    source = inspect.getsource(TaskSyncManager.import_from_jsonl)

    assert '"status":' not in source
    assert '"lifecycle_stage":' not in source
    assert '"validation_status": validation_status' in source
    assert '"task_type": data.get("task_type", "task")' in source


def test_import_ignores_top_level_legacy_keys() -> None:
    source = source_text("src/gobby/sync/tasks.py")

    assert "data.get('status')" not in source
    assert 'data.get("status")' not in source
    assert "data.get('lifecycle_stage')" not in source
    assert 'data.get("lifecycle_stage")' not in source
