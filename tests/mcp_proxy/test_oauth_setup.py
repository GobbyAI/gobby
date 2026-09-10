"""Expected OAuth setup conditions survive the real SDK connection boundary."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.client_manager import connections
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import ConnectionState, MCPAuthorizationRequired, MCPServerConfig
from gobby.mcp_proxy.services.server_mgmt import ServerManagementService
from gobby.mcp_proxy.transports.http import HTTPTransportConnection
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("project_id", ["project", GLOBAL_PROJECT_ID])
async def test_unauthed_add_exposes_scoped_command_without_error_logs(
    project_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    store = MagicMock(spec=SecretStore)
    store.get.return_value = None
    manager = MCPClientManager()

    async def connect(config: MCPServerConfig) -> object:
        return await connections.connect_server(
            manager, config, lambda cfg: HTTPTransportConnection(cfg, secret_store=store)
        )

    with (
        caplog.at_level(logging.INFO),
        patch.object(manager, "_connect_server", side_effect=connect),
    ):
        result = await ServerManagementService(manager, config_manager=MagicMock()).add_server(
            "fieldy",
            "http",
            url="https://never-contacted.invalid/mcp",
            project_id=project_id,
            scope="global" if project_id == GLOBAL_PROJECT_ID else "project",
            requires_oauth=True,
        )

    command = "gobby mcp-proxy auth fieldy"
    if project_id == GLOBAL_PROJECT_ID:
        command += " --global"
    assert result["success"] is True and result["connected"] is False
    assert result["needs_configuration"] is True
    assert result["configure"] == [command]
    assert "error" not in result
    state = manager.health[result["id"]]
    assert state.state == ConnectionState.NEEDS_CONFIGURATION
    assert state.last_error is not None and command in state.last_error
    assert result["id"] not in manager._connections
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    store.set.assert_not_called()


@pytest.mark.asyncio
async def test_consent_requirement_does_not_retry_or_trip_circuit_breaker() -> None:
    manager = MCPClientManager()
    config = MCPServerConfig(name="fieldy", project_id=GLOBAL_PROJECT_ID)
    manager._lazy_connector.register_server(config.id)
    required = MCPAuthorizationRequired("gobby mcp-proxy auth fieldy --global")
    with patch.object(manager, "_connect_server", new_callable=AsyncMock) as connect:
        connect.side_effect = required
        with pytest.raises(MCPAuthorizationRequired) as caught:
            await connections._connect_with_retries(manager, config.id, config)
    assert caught.value is required
    assert connect.await_count == 1
    assert manager._lazy_connector.can_attempt_connection(config.id)
