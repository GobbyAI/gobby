"""Tests for agent definition CLI commands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.storage.workflow_definitions import WorkflowDefinitionRow
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
) -> WorkflowDefinitionRow:
    body = AgentDefinitionBody(
        name=name,
        description=description,
        provider=provider,
        model=model,
        surfaces=surfaces or ["spawn"],
        role="Builder",
        timeout=120.0,
        max_turns=8,
        enabled=enabled,
    )
    return WorkflowDefinitionRow(
        id=f"wf-{name}",
        name=name,
        description=description,
        workflow_type="agent",
        enabled=enabled,
        priority=100,
        definition_json=body.model_dump_json(),
        source="installed",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
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
        manager.list_all.assert_called_once_with(workflow_type="agent", enabled=None)

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
        manager.list_all.assert_called_once_with(workflow_type="agent", enabled=True)

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
        assert data["max_turns"] == 8

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
