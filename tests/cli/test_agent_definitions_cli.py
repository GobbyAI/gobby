"""Tests for agent definition CLI commands."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.storage.definitions.agents import AgentDefinitionRow
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _agent_row(
    name: str,
    *,
    description: str | None = None,
    enabled: bool = True,
    provider: str = "claude",
    model: str | None = None,
    surfaces: list[str] | None = None,
    step_workflow: dict[str, Any] | None = None,
) -> AgentDefinitionRow:
    body = AgentDefinitionBody(
        name=name,
        description=description,
        provider=provider,
        model=model,
        surfaces=surfaces or ["spawn"],
        role="Builder",
        timeout=120.0,
        enabled=enabled,
        step_workflow=step_workflow,
    )
    return AgentDefinitionRow(
        id=f"wf-{name}",
        name=name,
        description=description,
        enabled=enabled,
        enabled_pinned=False,
        definition_json=body.model_dump(mode="json"),
        source="installed",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        step_workflow_id="sw-1" if step_workflow is not None else None,
    )


class TestAgentDefinitionsList:
    def test_help_shows_definition_filters(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["agents", "list", "--help"])

        assert result.exit_code == 0
        assert "--enabled" in result.output
        assert "--disabled" in result.output
        assert "--surface" in result.output
        assert "--session" not in result.output

    @patch("gobby.cli.agents.get_agent_definition_manager")
    def test_lists_agent_definitions(self, mock_get_manager: MagicMock, runner: CliRunner) -> None:
        manager = MagicMock()
        manager.list_all.return_value = [_agent_row("developer", description="Build features")]
        mock_get_manager.return_value = manager

        result = runner.invoke(cli, ["agents", "list"])

        assert result.exit_code == 0
        assert "Found 1 agent definition" in result.output
        assert "developer" in result.output
        assert "Build features" in result.output
        manager.list_all.assert_called_once_with(enabled=None)

    @patch("gobby.cli.agents.get_agent_definition_manager")
    def test_filters_by_enabled_and_surface(
        self, mock_get_manager: MagicMock, runner: CliRunner
    ) -> None:
        manager = MagicMock()
        manager.list_all.return_value = [
            _agent_row("spawn-only", surfaces=["spawn"]),
            _agent_row("persona-ready", surfaces=["spawn", "persona"]),
        ]
        mock_get_manager.return_value = manager

        result = runner.invoke(cli, ["agents", "list", "--enabled", "--surface", "persona"])

        assert result.exit_code == 0
        assert "persona-ready" in result.output
        assert "spawn-only" not in result.output
        manager.list_all.assert_called_once_with(enabled=True)

    @patch("gobby.cli.agents.get_agent_definition_manager")
    def test_json_output(self, mock_get_manager: MagicMock, runner: CliRunner) -> None:
        manager = MagicMock()
        manager.list_all.return_value = [_agent_row("developer", model="opus")]
        mock_get_manager.return_value = manager

        result = runner.invoke(cli, ["agents", "list", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1
        assert data["agents"][0]["name"] == "developer"
        assert data["agents"][0]["model"] == "opus"

    @patch("gobby.cli.agents.get_agent_definition_manager")
    def test_old_run_options_removed(self, mock_get_manager: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["agents", "list", "--status", "running"])

        assert result.exit_code == 2
        assert "No such option" in result.output
        mock_get_manager.assert_not_called()


class TestAgentDefinitionsShow:
    @patch("gobby.cli.agents.get_agent_definition_manager")
    def test_show_agent_definition(self, mock_get_manager: MagicMock, runner: CliRunner) -> None:
        manager = MagicMock()
        manager.get_by_name.return_value = _agent_row("developer", description="Build features")
        mock_get_manager.return_value = manager

        result = runner.invoke(cli, ["agents", "show", "developer"])

        assert result.exit_code == 0
        assert "Agent: developer" in result.output
        assert "Description: Build features" in result.output
        assert "Provider: claude" in result.output
        assert "Role:" in result.output

    @patch("gobby.cli.agents.get_agent_definition_manager")
    def test_show_json_output(self, mock_get_manager: MagicMock, runner: CliRunner) -> None:
        manager = MagicMock()
        manager.get_by_name.return_value = _agent_row("developer", surfaces=["spawn", "persona"])
        mock_get_manager.return_value = manager

        result = runner.invoke(cli, ["agents", "show", "developer", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "developer"
        assert data["surfaces"] == ["spawn", "persona"]
        assert "step_workflow" in data
        assert data["step_workflow"] is None
        assert "max_turns" not in data

    @patch("gobby.cli.agents.get_agent_definition_manager")
    def test_show_json_emits_nested_step_workflow(
        self, mock_get_manager: MagicMock, runner: CliRunner
    ) -> None:
        manager = MagicMock()
        manager.get_by_name.return_value = _agent_row(
            "developer",
            step_workflow={
                "variables": {"required_skills": ["tdd"]},
                "steps": [{"name": "implement"}],
            },
        )
        mock_get_manager.return_value = manager

        result = runner.invoke(cli, ["agents", "show", "developer", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["step_workflow"]["variables"]["required_skills"] == ["tdd"]
        assert data["step_workflow"]["steps"][0]["name"] == "implement"
        assert "steps" not in data

    @patch("gobby.cli.agents.get_agent_run_manager")
    @patch("gobby.cli.agents.get_agent_definition_manager")
    def test_old_run_show_behavior_removed(
        self,
        mock_get_definition_manager: MagicMock,
        mock_get_run_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        manager = MagicMock()
        manager.get_by_name.return_value = None
        mock_get_definition_manager.return_value = manager

        result = runner.invoke(cli, ["agents", "show", "run-abc"])

        assert result.exit_code == 1
        assert "Agent definition not found" in result.output
        mock_get_run_manager.assert_not_called()
