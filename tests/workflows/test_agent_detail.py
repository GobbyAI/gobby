"""Tests for the shared agent definition detail projection."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.storage.definitions.agents import AgentDefinitionRow
from gobby.workflows.agent_detail import HIDDEN_DETAIL_FIELDS, agent_definition_detail
from gobby.workflows.definitions import AgentDefinitionBody
from tests.fixtures.agent_definitions import make_agent_definition

pytestmark = pytest.mark.unit


def _row(body: AgentDefinitionBody) -> AgentDefinitionRow:
    definition = body.model_dump(mode="json")
    definition["mode"] = "legacy-mode"
    return AgentDefinitionRow(
        id="wf-reviewer",
        name=body.name,
        description=None,
        enabled=False,
        enabled_pinned=False,
        definition_json=definition,
        source="installed",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        tags=["review"],
    )


def test_detail_projects_every_definition_field_except_hidden_ones() -> None:
    body = make_agent_definition(
        name="reviewer",
        description="Read-only reviewer",
        prompts={"agent": "Review the task."},
        prewarm_pre_commit_store=False,
        blocked_mcp_tools=["gobby-tasks:close_task"],
        api_token="secret-token-value",
    )

    detail = agent_definition_detail(_row(body))

    assert HIDDEN_DETAIL_FIELDS == {"api_token"}
    assert set(AgentDefinitionBody.model_fields) - HIDDEN_DETAIL_FIELDS <= detail.keys()
    assert detail.keys().isdisjoint(HIDDEN_DETAIL_FIELDS)
    assert "secret-token-value" not in repr(detail)
    assert detail["prewarm_pre_commit_store"] is False
    assert detail["blocked_mcp_tools"] == ["gobby-tasks:close_task"]


def test_detail_overlays_row_metadata_on_the_body() -> None:
    body = make_agent_definition(
        name="reviewer",
        description="Read-only reviewer",
        prompts={"agent": "Review the task."},
    )

    detail = agent_definition_detail(_row(body))

    assert detail["id"] == "wf-reviewer"
    assert detail["description"] == "Read-only reviewer"
    assert detail["enabled"] is False
    assert detail["source"] == "installed"
    assert detail["project_id"] is None
    assert detail["tags"] == ["review"]
    assert detail["mode"] == "legacy-mode"
