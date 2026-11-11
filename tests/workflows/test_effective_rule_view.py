"""The effective rule view resolves project overrides over global rows."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

GATE = "synthetic-guarded-gate"
BODY: dict[str, Any] = {"event": "task_close", "effects": [{"type": "block", "reason": "g"}]}


def test_project_row_displaces_the_global_row(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = str(sample_project["id"])
    manager = RuleDefinitionManager(temp_db)
    manager.create(GATE, BODY, source="installed")
    manager.create(GATE, BODY, project_id=project_id, source="installed")

    effective = [row.project_id for row in manager.list_effective(project_id) if row.name == GATE]

    assert effective == [project_id]


@pytest.mark.parametrize("with_global", [False, True], ids=["foreign-only", "foreign-and-global"])
def test_global_view_excludes_other_projects_rows(
    temp_db: HubDatabase, sample_project: dict[str, Any], with_global: bool
) -> None:
    manager = RuleDefinitionManager(temp_db)
    manager.create(GATE, BODY, project_id=str(sample_project["id"]), source="installed")
    if with_global:
        manager.create(GATE, BODY, source="installed")

    effective = [row.project_id for row in manager.list_effective(None) if row.name == GATE]

    assert effective == ([None] if with_global else [])
