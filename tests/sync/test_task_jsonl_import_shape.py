"""Task JSONL import ignores legacy task state keys."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def test_import_does_not_write_legacy_columns() -> None:
    source = source_text("src/gobby/sync/tasks.py")

    assert "INSERT INTO tasks" not in source or "status" not in source
    assert "UPDATE tasks SET" not in source or "lifecycle_stage" not in source


def test_import_ignores_top_level_legacy_keys() -> None:
    source = source_text("src/gobby/sync/tasks.py")

    assert "data.get('status')" not in source
    assert 'data.get("status")' not in source
    assert "data.get('lifecycle_stage')" not in source
    assert 'data.get("lifecycle_stage")' not in source
