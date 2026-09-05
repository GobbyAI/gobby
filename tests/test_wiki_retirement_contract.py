"""Public Python surfaces after retirement of the legacy wiki."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.cli import cli
from gobby.config.app import DaemonConfig
from gobby.config.registry import CONFIG_REGISTRY, UnknownConfigKeyError
from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.servers._app_routes import register_routes
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


def test_mcp_discovery_preserves_shared_registries_without_wiki(hub_db: HubDatabase) -> None:
    config = DaemonConfig()
    manager = setup_internal_registries(
        config_resolver=lambda: config, db=hub_db, session_manager=MagicMock()
    )
    names = {registry.name for registry in manager.get_all_registries()}
    assert {"gobby-workflows", "gobby-hub", "gobby-sessions"} <= names
    assert "gobby-wiki" not in names
    assert manager.get_registry("gobby-wiki") is None


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/wiki/status"),
        ("GET", "/api/wiki/search?q=legacy"),
        ("GET", "/api/wiki/code/status"),
        ("POST", "/api/wiki/code/refresh"),
        ("POST", "/api/wiki/compile"),
    ],
)
def test_retired_http_routes_use_normal_missing_route(method: str, path: str) -> None:
    server = MagicMock()
    app = FastAPI()
    register_routes(app, server)
    with TestClient(app) as client:
        missing = client.request(method, "/api/unknown-retirement-probe")
        retired = client.request(method, path)
    assert missing.status_code == 404
    assert retired.status_code == missing.status_code
    assert retired.json() == missing.json()
    assert not any(path.startswith("/api/wiki") for path in app.openapi()["paths"])
    assert "/api/code-index/graph/clear" in app.openapi()["paths"]


def test_wiki_configuration_is_absent_from_python_and_native_contracts() -> None:
    assert "wiki" not in DaemonConfig.model_fields
    assert "wiki" not in DaemonConfig().model_dump()
    with pytest.raises(UnknownConfigKeyError):
        CONFIG_REGISTRY.resolve("wiki.enabled")
    assert CONFIG_REGISTRY.resolve("code_index.enabled").key == "code_index.enabled"
    root = Path(__file__).resolve().parents[1]
    contract = json.loads(
        (root / "crates/gcore/assets/config/runtime_config_contract.json").read_text()
    )
    entries = contract["exactKeys"] + contract["patterns"]
    assert not any(entry["namespace"] == "wiki" for entry in entries)
    assert any(entry["namespace"] == "code_index" for entry in entries)


def test_removed_wiki_hook_command_is_unavailable() -> None:
    runner = CliRunner()
    help_result = runner.invoke(cli, ["hooks", "--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "resolve-wiki-vault" not in help_result.output
    result = runner.invoke(cli, ["hooks", "resolve-wiki-vault"])
    assert result.exit_code == 2
    assert "No such command 'resolve-wiki-vault'" in result.output
