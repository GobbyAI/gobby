"""Expected OAuth setup conditions survive the real SDK connection boundary."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2
import pytest
from mcp.client.auth import OAuthFlowError, OAuthRegistrationError, OAuthTokenError

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


@pytest.mark.asyncio
async def test_first_connection_authorizes_and_retries() -> None:
    config = MCPServerConfig(
        name="fieldy",
        project_id=GLOBAL_PROJECT_ID,
        transport="http",
        url="https://api.fieldy.ai/mcp",
        requires_oauth=True,
    )
    authorized = False

    async def authorize(_config: MCPServerConfig, _store: SecretStore) -> None:
        nonlocal authorized
        authorized = True

    authorizer = AsyncMock(side_effect=authorize)
    db_manager = MagicMock()
    db_manager.db = MagicMock()
    manager = MCPClientManager(
        [config],
        mcp_db_manager=db_manager,
        max_connection_retries=0,
        oauth_authorizer=authorizer,
    )
    session = MagicMock()
    required = MCPAuthorizationRequired("gobby mcp-proxy auth fieldy --global")

    async def connect(_config: MCPServerConfig) -> object:
        if not authorized:
            raise required
        connection = MagicMock(is_connected=True, session=session)
        manager._connections[config.id] = connection
        return session

    with patch.object(manager, "_connect_server", side_effect=connect) as connect_mock:
        result = await manager.ensure_connected(config.id)

    assert result is session
    assert connect_mock.await_count == 2
    authorizer.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_first_connections_share_authorization() -> None:
    config = MCPServerConfig(
        name="fieldy",
        project_id=GLOBAL_PROJECT_ID,
        transport="http",
        url="https://api.fieldy.ai/mcp",
        requires_oauth=True,
    )
    auth_started = asyncio.Event()
    release_auth = asyncio.Event()
    both_challenged = asyncio.Event()
    authorized = False
    connect_count = 0

    async def authorize(_config: MCPServerConfig, _store: SecretStore) -> None:
        nonlocal authorized
        auth_started.set()
        await release_auth.wait()
        authorized = True

    authorizer = AsyncMock(side_effect=authorize)
    db_manager = MagicMock()
    db_manager.db = MagicMock()
    manager = MCPClientManager(
        [config],
        mcp_db_manager=db_manager,
        max_connection_retries=0,
        oauth_authorizer=authorizer,
    )
    session = MagicMock()
    required = MCPAuthorizationRequired("gobby mcp-proxy auth fieldy --global")

    async def connect(_config: MCPServerConfig) -> object:
        nonlocal connect_count
        connect_count += 1
        if not authorized:
            if connect_count == 2:
                both_challenged.set()
            raise required
        connection = MagicMock(is_connected=True, session=session)
        manager._connections[config.id] = connection
        return session

    with patch.object(manager, "_connect_server", side_effect=connect):
        first = asyncio.create_task(manager.ensure_connected(config.id))
        await asyncio.wait_for(auth_started.wait(), timeout=1)
        second = asyncio.create_task(manager.ensure_connected(config.id))
        await asyncio.wait_for(both_challenged.wait(), timeout=1)
        release_auth.set()
        first_result, second_result = await asyncio.gather(first, second)

    assert first_result is session
    assert second_result is session
    assert connect_count == 3
    authorizer.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_first_connections_share_failed_authorization() -> None:
    config = MCPServerConfig(
        name="fieldy",
        project_id=GLOBAL_PROJECT_ID,
        transport="http",
        url="https://api.fieldy.ai/mcp",
        requires_oauth=True,
    )
    auth_started = asyncio.Event()
    release_auth = asyncio.Event()
    both_challenged = asyncio.Event()
    connect_count = 0

    async def authorize(_config: MCPServerConfig, _store: SecretStore) -> None:
        auth_started.set()
        await release_auth.wait()
        raise RuntimeError("browser unavailable")

    authorizer = AsyncMock(side_effect=authorize)
    db_manager = MagicMock()
    db_manager.db = MagicMock()
    manager = MCPClientManager(
        [config],
        mcp_db_manager=db_manager,
        max_connection_retries=0,
        oauth_authorizer=authorizer,
    )
    required = MCPAuthorizationRequired("gobby mcp-proxy auth fieldy --global")

    async def connect(_config: MCPServerConfig) -> object:
        nonlocal connect_count
        connect_count += 1
        if connect_count == 2:
            both_challenged.set()
        raise required

    with patch.object(manager, "_connect_server", side_effect=connect):
        first = asyncio.create_task(manager.ensure_connected(config.id))
        await asyncio.wait_for(auth_started.wait(), timeout=1)
        second = asyncio.create_task(manager.ensure_connected(config.id))
        await asyncio.wait_for(both_challenged.wait(), timeout=1)
        release_auth.set()
        with pytest.raises(MCPAuthorizationRequired):
            await first
        with pytest.raises(MCPAuthorizationRequired):
            await second

    assert connect_count == 2
    authorizer.assert_awaited_once()


@pytest.mark.asyncio
async def test_automatic_authorization_failure_preserves_manual_command() -> None:
    config = MCPServerConfig(
        name="fieldy",
        project_id=GLOBAL_PROJECT_ID,
        transport="http",
        url="https://api.fieldy.ai/mcp",
        requires_oauth=True,
    )
    authorizer = AsyncMock(side_effect=RuntimeError("browser unavailable"))
    db_manager = MagicMock()
    db_manager.db = MagicMock()
    manager = MCPClientManager(
        [config],
        mcp_db_manager=db_manager,
        max_connection_retries=0,
        oauth_authorizer=authorizer,
    )
    required = MCPAuthorizationRequired("gobby mcp-proxy auth fieldy --global")

    with patch.object(manager, "_connect_server", side_effect=required) as connect:
        with pytest.raises(MCPAuthorizationRequired) as caught:
            await manager.ensure_connected(config.id)

    assert caught.value is required
    assert caught.value.command == "gobby mcp-proxy auth fieldy --global"
    assert connect.await_count == 1
    authorizer.assert_awaited_once()


@pytest.mark.asyncio
async def test_automatic_authorization_failure_logs_nested_exception_group_leaf_types(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = MCPServerConfig(
        name="fieldy",
        project_id=GLOBAL_PROJECT_ID,
        transport="http",
        url="https://api.fieldy.ai/mcp",
        requires_oauth=True,
    )
    failure = BaseExceptionGroup(
        "outer authorization failure",
        [
            OAuthRegistrationError("registration failed"),
            BaseExceptionGroup(
                "nested authorization failure",
                [
                    OAuthFlowError("callback failed"),
                    OAuthTokenError("token exchange failed"),
                    httpx2.ConnectError("transport failed"),
                ],
            ),
        ],
    )
    db_manager = MagicMock()
    db_manager.db = MagicMock()
    manager = MCPClientManager(
        [config],
        mcp_db_manager=db_manager,
        max_connection_retries=0,
        oauth_authorizer=AsyncMock(side_effect=failure),
    )
    required = MCPAuthorizationRequired("gobby mcp-proxy auth fieldy --global")

    with (
        patch.object(manager, "_connect_server", side_effect=required),
        caplog.at_level(logging.WARNING, logger="gobby.mcp.manager"),
        pytest.raises(MCPAuthorizationRequired),
    ):
        await manager.ensure_connected(config.id)

    assert [record.getMessage() for record in caplog.records] == [
        "Automatic OAuth authorization failed for 'fieldy': "
        "ExceptionGroup[OAuthRegistrationError, "
        "ExceptionGroup[OAuthFlowError, OAuthTokenError, ConnectError]]"
    ]


@pytest.mark.asyncio
async def test_automatic_authorization_failure_logs_types_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secrets = {
        "exception message",
        "error_description=private-description",
        "code=private-authorization-code",
        "token=private-access-token",
        "client_secret=private-client-secret",
    }
    config = MCPServerConfig(
        name="fieldy",
        project_id=GLOBAL_PROJECT_ID,
        transport="http",
        url="https://api.fieldy.ai/mcp",
        requires_oauth=True,
    )
    failure = BaseExceptionGroup(
        " ".join(secrets),
        [OAuthTokenError(*sorted(secrets))],
    )
    db_manager = MagicMock()
    db_manager.db = MagicMock()
    manager = MCPClientManager(
        [config],
        mcp_db_manager=db_manager,
        max_connection_retries=0,
        oauth_authorizer=AsyncMock(side_effect=failure),
    )
    required = MCPAuthorizationRequired("gobby mcp-proxy auth fieldy --global")

    with (
        patch.object(manager, "_connect_server", side_effect=required),
        caplog.at_level(logging.WARNING, logger="gobby.mcp.manager"),
        pytest.raises(MCPAuthorizationRequired),
    ):
        await manager.ensure_connected(config.id)

    assert [record.getMessage() for record in caplog.records] == [
        "Automatic OAuth authorization failed for 'fieldy': ExceptionGroup[OAuthTokenError]"
    ]
    assert all(secret not in caplog.text for secret in secrets)
    assert "Traceback" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
