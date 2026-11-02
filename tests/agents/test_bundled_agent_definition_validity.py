"""Bundled agent YAML files must parse as valid agent definitions.

The only test that reads the bundled agent directory. It runs the same parse
step as sync_bundled_agents: safe-load, default the name to the file stem, then
validate through AgentDefinitionBody.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from gobby.agents.sync import get_bundled_agents_path
from gobby.cli.agents_steps import check_agent
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit

BUNDLED_AGENT_YAMLS = sorted(get_bundled_agents_path().rglob("*.yaml"))


def _validate_agent_definition(path: Path) -> AgentDefinitionBody:
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} is not a YAML mapping"
    name = data.get("name")
    data["name"] = name.strip() if isinstance(name, str) and name.strip() else path.stem
    return AgentDefinitionBody.model_validate(data)


@pytest.mark.parametrize("path", BUNDLED_AGENT_YAMLS, ids=lambda path: path.name)
def test_bundled_agent_yaml_is_valid_agent_definition(path: Path) -> None:
    definition = _validate_agent_definition(path)

    assert definition.name, f"{path.name} parsed without a name"


@pytest.mark.parametrize("path", BUNDLED_AGENT_YAMLS, ids=lambda path: path.name)
def test_agents_check_reports_valid_for_every_bundled_definition(
    path: Path, temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _validate_agent_definition(path)
    AgentDefinitionManager(temp_db).upsert_from_sync(
        body.name,
        body.model_dump(mode="json"),
        body.step_workflow.model_dump(mode="json") if body.step_workflow else None,
    )
    monkeypatch.setattr("gobby.cli.agents_steps.require_cli_database", lambda: temp_db)
    monkeypatch.setattr("gobby.cli.agents_steps.get_project_context", lambda **kwargs: None)
    result = CliRunner().invoke(check_agent, [body.name])
    assert result.exit_code == 0, result.output
    assert "VALID" in result.output, result.output


def test_synthetic_definition_with_invalid_field_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "invalid-definition.yaml"
    path.write_text("name: invalid-definition\nworkflows: {}\nenabled: maybe-not\n")

    with pytest.raises(ValidationError) as excinfo:
        _validate_agent_definition(path)

    error_locations = [error["loc"] for error in excinfo.value.errors()]
    assert ("enabled",) in error_locations
