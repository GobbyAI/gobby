"""OAuth settings survive service creation and registry updates."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.shared.auth import OAuthToken

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.oauth import MCPOAuthStorage
from gobby.mcp_proxy.services.server_mgmt import ServerManagementService
from gobby.servers.routes.dependencies import get_metrics_manager, get_server
from gobby.servers.routes.mcp.tools import create_mcp_router
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_oauth_config_persistence_and_runtime_patch(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    registry = LocalMCPManager(temp_db)
    manager = MCPClientManager(project_id=project_id, mcp_db_manager=registry)
    service = ServerManagementService(manager, config_manager=MagicMock())
    added = await service.add_server(
        "oauth-server",
        "http",
        url="https://resource.example/mcp",
        enabled=False,
        project_id=project_id,
        requires_oauth=True,
        connect_timeout=42.0,
    )
    assert added["success"] is True
    row = registry.get_server("oauth-server", project_id)
    assert row is not None and row.requires_oauth and row.connect_timeout == 42.0

    # Mapping updates previously silently ignored all runtime fields, including OAuth.
    await manager.update_server(
        row.id,
        {
            "requires_oauth": False,
            "url": "https://resource.example/new-mcp",
            "headers": {"X-Tenant": "test"},
            "connect_timeout": 25.0,
        },
    )
    updated = registry.get_server_by_id(row.id)
    assert updated is not None and not updated.requires_oauth
    assert updated.url == "https://resource.example/new-mcp"
    assert updated.headers == {"X-Tenant": "test"}
    assert updated.connect_timeout == 25.0
    assert not updated.enabled
    await manager.update_server(row.id, {"requires_oauth": True})
    restarted = MCPClientManager(project_id=project_id, mcp_db_manager=registry)
    config = restarted.get_server_config(row.id)
    assert config is not None and config.requires_oauth

    store = SecretStore(temp_db, gobby_home=tmp_path)
    storage = MCPOAuthStorage(store, config)
    await storage.set_tokens(OAuthToken(access_token="private-access", token_type="Bearer"))
    await storage.save()
    record = temp_db.fetchone(
        "SELECT encrypted_value FROM secrets WHERE name = %s", (storage.name,)
    )
    assert record is not None and "private-access" not in record["encrypted_value"]
    restored = MCPOAuthStorage(SecretStore(temp_db, gobby_home=tmp_path), config)
    await restored.load()
    assert restored.state.tokens is not None
    assert restored.state.tokens.access_token == "private-access"


@pytest.mark.asyncio
async def test_invalid_oauth_patch_leaves_stored_config_unchanged(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = str(sample_project["id"])
    registry = LocalMCPManager(temp_db)
    manager = MCPClientManager(project_id=project_id, mcp_db_manager=registry)
    await ServerManagementService(manager, config_manager=MagicMock()).add_server(
        "oauth-server",
        "http",
        url="https://resource.example/mcp",
        enabled=False,
        project_id=project_id,
    )
    row = registry.get_server("oauth-server", project_id)
    assert row is not None
    with pytest.raises(ValueError, match="requires_oauth must be a boolean"):
        await manager.update_server(row.id, {"requires_oauth": "false"})
    saved = registry.get_server_by_id(row.id)
    assert saved is not None and not saved.requires_oauth


def test_http_api_forwards_oauth_and_runtime_updates(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = str(sample_project["id"])
    registry = LocalMCPManager(temp_db)
    manager = MCPClientManager(project_id=project_id, mcp_db_manager=registry)
    server = MagicMock()
    server.mcp_manager = manager
    server.session_manager = None
    server.services.websocket_server = None
    app = FastAPI()
    app.include_router(create_mcp_router())
    app.dependency_overrides[get_server] = lambda: server
    app.dependency_overrides[get_metrics_manager] = lambda: None
    with TestClient(app) as client:
        response = client.post(
            "/api/mcp/servers",
            json={
                "name": "fieldy",
                "transport": "http",
                "url": "https://api.fieldy.ai/mcp",
                "requires_oauth": True,
                "enabled": False,
                "project_id": project_id,
            },
        )
        assert response.status_code == 200 and response.json()["success"]
        row = registry.get_server("fieldy", project_id)
        assert row is not None and row.requires_oauth
        response = client.put(
            "/api/mcp/servers/fieldy",
            json={
                "requires_oauth": False,
                "project_id": project_id,
            },
        )
        assert response.status_code == 200 and response.json()["success"]
        row = registry.get_server("fieldy", project_id)
        assert row is not None and not row.requires_oauth
