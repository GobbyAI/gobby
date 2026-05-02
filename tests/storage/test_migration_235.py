"""Phase 5 migration 235 task-type default manifest contracts."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import NEW_TASK_TYPES, migration_action

pytestmark = pytest.mark.unit


def test_new_task_type_defaults(temp_db) -> None:
    migration_action(235)

    rows = temp_db.fetchall(
        """
        SELECT task_type, stage_name, position
          FROM task_type_default_stages
         WHERE task_type IN (?, ?, ?, ?)
         ORDER BY task_type, position
        """,
        NEW_TASK_TYPES,
    )
    by_type: dict[str, list[str]] = {}
    for row in rows:
        by_type.setdefault(row["task_type"], []).append(row["stage_name"])

    assert set(by_type) == set(NEW_TASK_TYPES)
    assert by_type["simple_fix"] == ["development", "pr", "merge"]
    assert by_type["research_spike"] == ["ideation", "research", "prd"]
    assert by_type["prd_doc"] == ["ideation", "prd"]
    assert by_type["architecture_doc"] == ["research", "architecture"]
