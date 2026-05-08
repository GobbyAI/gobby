"""Task aggregate queries must use stage-native projections."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def test_aggregates_does_not_import_state_sql() -> None:
    assert "_state_sql" not in source_text("src/gobby/storage/tasks/_aggregates.py")


def test_projected_status_built_from_current_stage() -> None:
    source = source_text("src/gobby/storage/tasks/_aggregates.py")

    assert "current_stage" in source
    assert "canonical_status_case" not in source


def test_ready_predicate_matches_list_ready_tasks_projection() -> None:
    source = source_text("src/gobby/storage/tasks/_aggregates.py")

    assert "task_stage_states" in source
    assert "is_ready_sql" not in source
