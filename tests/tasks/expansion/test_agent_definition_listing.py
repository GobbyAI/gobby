"""Expansion agent selection resolves definitions the way spawn does."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.definitions.agents import AgentDefinitionRow
from gobby.tasks.expansion._common import list_agent_definitions
from tests.fixtures.agent_definitions import make_agent_definition

pytestmark = pytest.mark.unit

_PROJECT = "6c1f7a52-3b0e-4d4a-9a55-2f0b8f1d9e01"


def _row(name: str, *, project_id: str | None, enabled: bool = True) -> AgentDefinitionRow:
    body = make_agent_definition(prompts={"agent": "Review the task."}, name=name, enabled=enabled)
    return AgentDefinitionRow(
        id=f"wf-{name}-{project_id or 'global'}",
        name=name,
        description=None,
        enabled=enabled,
        enabled_pinned=False,
        definition_json=body.model_dump(mode="json"),
        source="installed",
        project_id=project_id,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        step_workflow_id=None,
    )


class _ShadowingManager:
    """Global and project rows for one name; the project row is what spawn resolves."""

    def __init__(self, *, project_enabled: bool) -> None:
        self.global_row = _row("reviewer", project_id=None)
        self.project_row = _row("reviewer", project_id=_PROJECT, enabled=project_enabled)

    def list_all(
        self, enabled: bool | None = None, project_id: str | None = None
    ) -> list[AgentDefinitionRow]:
        rows = [self.global_row, self.project_row]
        return [row for row in rows if enabled is None or row.enabled is enabled]

    def list_resolved(self, project_id: str | None = None) -> list[AgentDefinitionRow]:
        return [self.project_row] if project_id == _PROJECT else [self.global_row]


def _list(manager: _ShadowingManager) -> list[tuple[str, str | None]]:
    result = list_agent_definitions(
        cast(AgentDefinitionManager, manager),
        enabled=True,
        project_id=_PROJECT,
        surface_filter="spawn",
    )
    agents: list[dict[str, Any]] = result["agents"]
    return [(agent["id"], agent["project_id"]) for agent in agents]


def test_project_override_shadows_global_definition() -> None:
    assert _list(_ShadowingManager(project_enabled=True)) == [
        (f"wf-reviewer-{_PROJECT}", _PROJECT),
    ]


def test_disabled_project_override_hides_enabled_global_definition() -> None:
    assert _list(_ShadowingManager(project_enabled=False)) == []
