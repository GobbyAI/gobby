"""HTTP template listing and disabled-template parity (plan 4.3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.cli.mcp_proxy import mcp_proxy
from gobby.mcp_proxy.stdio_proxy import DaemonProxy
from gobby.servers.routes.dependencies import get_metrics_manager, get_server
from gobby.servers.routes.mcp.tools import create_mcp_router
from tests.mcp_proxy.services.test_scope_resolution_matrix import PROJECT_ID
from tests.mcp_proxy.test_stdio_proxy import _capture_stdio_tools, _response

pytestmark = pytest.mark.unit

_HTTP_ENDPOINTS_DOC = Path(__file__).resolve().parents[4] / "docs" / "guides" / "http-endpoints.md"


def _client(server: Any) -> TestClient:
    app = FastAPI()
    app.include_router(create_mcp_router())

    async def override_server() -> Any:
        return server

    app.dependency_overrides[get_server] = override_server
    app.dependency_overrides[get_metrics_manager] = lambda: None
    return TestClient(app)


def test_list_mcp_templates_returns_project_visible_contracts() -> None:
    row = MagicMock()
    row.name = "demo"
    row.owner = "gobby"
    row.project_id = PROJECT_ID
    row.definition = {
        "description": "Demo MCP template",
        "params": [{"name": "region", "env": "REGION", "required": True}],
    }
    db = MagicMock()
    db.list_templates.return_value = [row]
    manager = MagicMock()
    manager.mcp_db_manager = db
    server = MagicMock()
    server.mcp_manager = manager
    server._mcp_db_manager = db
    server.session_manager = None
    client = _client(server)

    response = client.get("/api/mcp/templates", params={"project_id": PROJECT_ID})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is not False
    templates = payload["templates"]
    assert templates[0]["name"] == "demo"
    assert templates[0]["owner"] == "gobby"
    assert templates[0]["scope"] == "project"
    assert templates[0]["params"][0]["name"] == "region"
    db.list_templates.assert_called()


def test_http_endpoints_documents_scoped_servers_and_templates() -> None:
    text = _HTTP_ENDPOINTS_DOC.read_text(encoding="utf-8")
    assert "/api/mcp/servers" in text
    assert "/api/mcp/templates" in text
    assert "missing_secrets" in text
    assert "scope" in text
    assert "template" in text


@pytest.mark.asyncio
async def test_disabled_template_instantiation_parity_across_adapters() -> None:
    disabled = {
        "success": False,
        "error": "template_disabled",
        "template": "disabled-tmpl",
        "scope": "project",
    }
    db = MagicMock()
    template_row = MagicMock()
    template_row.name = "disabled-tmpl"
    template_row.enabled = False
    template_row.project_id = PROJECT_ID
    db.get_template.return_value = template_row
    db.insert_server.side_effect = AssertionError("disabled template must not persist")
    manager = MagicMock()
    manager.mcp_db_manager = db
    manager.add_server = AsyncMock()
    server = MagicMock()
    server.mcp_manager = manager
    server._mcp_db_manager = db
    server.session_manager = None
    server.services.websocket_server = None
    client = _client(server)

    http = client.post(
        "/api/mcp/servers",
        json={
            "name": "disabled-instance",
            "template": "disabled-tmpl",
            "values": {},
            "project_id": PROJECT_ID,
        },
    )
    http_body = http.json()
    http_detail = http_body.get("detail", http_body)
    assert http_detail.get("error") == "template_disabled"
    manager.add_server.assert_not_called()
    db.insert_server.assert_not_called()

    runner = CliRunner()
    daemon = MagicMock()
    daemon.check_health.return_value = (True, None)
    daemon.call_http_api.return_value.status_code = 200
    daemon.call_http_api.return_value.json.return_value = disabled
    with (
        patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=daemon),
        patch(
            "gobby.cli.installers.shared.registered_project_id",
            return_value=PROJECT_ID,
        ),
        patch("gobby.cli.mcp_proxy.require_cli_database", return_value=MagicMock()),
    ):
        cli = runner.invoke(
            mcp_proxy,
            ["add-server", "disabled-instance", "--template", "disabled-tmpl"],
            obj={"config": MagicMock()},
        )
    assert "template_disabled" in cli.output
    assert cli.exit_code != 0 or "template_disabled" in cli.output

    proxy = DaemonProxy(60887)
    httpx_client = MagicMock()
    httpx_client.request = AsyncMock(return_value=_response(200, disabled))
    with patch("gobby.mcp_proxy.stdio_proxy.httpx.AsyncClient", return_value=httpx_client):
        stdio = await proxy.add_mcp_server(
            name="disabled-instance",
            template="disabled-tmpl",
            values={},
            scope="project",
        )
    assert stdio["error"] == "template_disabled"
    posted = httpx_client.request.await_args.kwargs["json"]
    assert posted["template"] == "disabled-tmpl"

    tools = _capture_stdio_tools(MagicMock(add_mcp_server=AsyncMock(return_value=disabled)))
    mcp_result = await tools["add_mcp_server"](
        name="disabled-instance",
        template="disabled-tmpl",
        values={},
        scope="project",
    )
    assert mcp_result["error"] == "template_disabled"
