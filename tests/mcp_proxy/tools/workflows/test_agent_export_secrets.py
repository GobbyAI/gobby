"""Agent YAML exports preserve references and refuse literal endpoint credentials."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gobby.mcp_proxy.tools.workflows._agents import _export_row
from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from tests.fixtures.agent_definitions import make_agent_definition

pytest_plugins = ["tests.storage.definitions.conftest"]
pytestmark = pytest.mark.unit


@pytest.mark.parametrize("boundary", ["agent-row", "dict-payload", "json-payload"])
@pytest.mark.parametrize("token", ["TEST-ONLY-LITERAL-TOKEN", "${KEY:-TEST-ONLY-DEFAULT-TOKEN}"])
def test_literal_endpoint_token_never_reaches_yaml_or_diagnostics(
    definition_db: PostgresHubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    boundary: str,
    token: str,
) -> None:
    monkeypatch.setattr("gobby.utils.dev.is_dev_mode", lambda _path: False)
    body = make_agent_definition(
        name="endpoint-agent",
        api_token=token,
        prompts={"agent": "Run the assigned task.", "persona": "Interactive guidance."},
    )
    definition = body.model_dump(mode="json")
    manager = AgentDefinitionManager(definition_db)
    row = manager.upsert_with_steps("endpoint-agent", definition, definition.get("step_workflow"))

    with pytest.raises(ValueError) as failure:
        exportable = (
            _export_row(row)
            if boundary == "agent-row"
            else SimpleNamespace(
                name=row.name,
                tags=["user"],
                definition_json=definition
                if boundary == "dict-payload"
                else json.dumps(definition),
            )
        )
        auto_export_definition(exportable, tmp_path, kind="agent")

    diagnostic = str(failure.value)
    assert "endpoint-agent" in diagnostic
    assert "api_token" in diagnostic
    assert token not in diagnostic
    assert token not in caplog.text
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err
    assert not list(tmp_path.rglob("*.yaml"))


@pytest.mark.parametrize("token", [None, "", "${LM_STUDIO_API_KEY}"])
def test_endpoint_token_reference_round_trips_unchanged(
    definition_db: PostgresHubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
) -> None:
    monkeypatch.setattr("gobby.utils.dev.is_dev_mode", lambda _path: False)
    body = make_agent_definition(
        name="endpoint-agent",
        api_token=token,
        prompts={"agent": "Run the assigned task.", "persona": "Interactive guidance."},
    )
    definition = body.model_dump(mode="json")
    row = AgentDefinitionManager(definition_db).upsert_with_steps(
        "endpoint-agent", definition, definition.get("step_workflow")
    )

    output = auto_export_definition(_export_row(row), tmp_path, kind="agent")

    assert output is not None
    exported = yaml.safe_load(output.read_text())
    assert exported.get("api_token") == token
