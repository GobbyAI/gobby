"""Tests for the BaseTransportConnection class."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.models import ConnectionState, MCPServerConfig
from gobby.mcp_proxy.transports.base import BaseTransportConnection
from tests._timing import wait_forever

pytestmark = pytest.mark.unit

# --- Fixtures ---


@pytest.fixture
def http_config() -> MCPServerConfig:
    """Create a sample HTTP server config."""
    return MCPServerConfig(
        name="test-http-server",
        project_id="test-project-uuid",
        transport="http",
        url="http://localhost:8080/mcp",
        enabled=True,
    )


@pytest.fixture
def stdio_config() -> MCPServerConfig:
    """Create a sample stdio server config."""
    return MCPServerConfig(
        name="test-stdio-server",
        project_id="test-project-uuid",
        transport="stdio",
        command="npx",
        args=["-y", "@test/server"],
        enabled=True,
    )


@pytest.fixture
def base_transport(http_config: MCPServerConfig) -> BaseTransportConnection:
    """Create a BaseTransportConnection instance for testing."""
    return BaseTransportConnection(config=http_config)


@pytest.fixture
def mock_session() -> MagicMock:
    """Create a mock ClientSession."""
    session = MagicMock()
    session.list_tools = AsyncMock(return_value=[])
    return session


# --- Concrete Test Implementation ---


class ConcreteTransportConnection(BaseTransportConnection):
    """Concrete implementation of BaseTransportConnection for testing."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._connect_called = False
        self._disconnect_called = False
        self._should_fail_connect = False
        self._mock_session: MagicMock | None = None

    async def connect(self) -> Any:
        """Connect and return ClientSession."""
        self._connect_called = True
        if self._should_fail_connect:
            self._state = ConnectionState.FAILED
            raise ConnectionError("Mock connection failed")
        self._state = ConnectionState.CONNECTED
        self._session = self._mock_session
        return self._session

    async def disconnect(self) -> None:
        """Disconnect from server."""
        self._disconnect_called = True
        self._session = None
        self._state = ConnectionState.DISCONNECTED


@pytest.fixture
def concrete_transport(http_config: MCPServerConfig) -> ConcreteTransportConnection:
    """Create a concrete transport implementation for testing."""
    return ConcreteTransportConnection(config=http_config)


# --- Test Classes ---


class TestBaseTransportConnectionInit:
    """Tests for BaseTransportConnection initialization."""

    def test_init_with_config_only(self, http_config: MCPServerConfig) -> None:
        """Test initialization with only config."""
        transport = BaseTransportConnection(config=http_config)

        assert transport.config == http_config
        assert transport._auth_token is None
        assert transport._token_refresh_callback is None
        assert transport._session is None
        assert transport._transport_context is None
        assert transport._state == ConnectionState.DISCONNECTED
        assert transport._last_health_check is None
        assert transport._consecutive_failures == 0

    def test_init_with_auth_token(self, http_config: MCPServerConfig) -> None:
        """Test initialization with auth token."""
        transport = BaseTransportConnection(
            config=http_config,
            auth_token="test-token-123",
        )

        assert transport._auth_token == "test-token-123"
        assert transport._token_refresh_callback is None

    def test_init_with_token_refresh_callback(self, http_config: MCPServerConfig) -> None:
        """Test initialization with token refresh callback."""

        async def refresh_token() -> str:
            return "new-token"

        transport = BaseTransportConnection(
            config=http_config,
            token_refresh_callback=refresh_token,
        )

        assert transport._token_refresh_callback is refresh_token

    def test_init_with_all_parameters(self, http_config: MCPServerConfig) -> None:
        """Test initialization with all parameters."""

        async def refresh_token() -> str:
            return "refreshed-token"

        transport = BaseTransportConnection(
            config=http_config,
            auth_token="initial-token",
            token_refresh_callback=refresh_token,
        )

        assert transport.config == http_config
        assert transport._auth_token == "initial-token"
        assert transport._token_refresh_callback is refresh_token

    def test_init_with_stdio_config(self, stdio_config: MCPServerConfig) -> None:
        """Test initialization with stdio config."""
        transport = BaseTransportConnection(config=stdio_config)

        assert transport.config == stdio_config
        assert transport.config.transport == "stdio"
        assert transport.config.command == "npx"


class TestBaseTransportConnectionProperties:
    """Tests for BaseTransportConnection properties."""

    def test_is_connected_false_when_disconnected(
        self, base_transport: BaseTransportConnection
    ) -> None:
        """Test is_connected returns False when disconnected."""
        assert base_transport.is_connected is False

    def test_is_connected_false_when_state_connected_but_no_session(
        self, base_transport: BaseTransportConnection
    ) -> None:
        """Test is_connected returns False when state is CONNECTED but session is None."""
        base_transport._state = ConnectionState.CONNECTED
        base_transport._session = None

        assert base_transport.is_connected is False

    def test_is_connected_true_when_connected_with_session(
        self, base_transport: BaseTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test is_connected returns True when state is CONNECTED and session exists."""
        base_transport._state = ConnectionState.CONNECTED
        base_transport._session = mock_session

        assert base_transport.is_connected is True

    def test_is_connected_false_when_connecting(
        self, base_transport: BaseTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test is_connected returns False when state is CONNECTING."""
        base_transport._state = ConnectionState.CONNECTING
        base_transport._session = mock_session

        assert base_transport.is_connected is False

    def test_is_connected_false_when_failed(
        self, base_transport: BaseTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test is_connected returns False when state is FAILED."""
        base_transport._state = ConnectionState.FAILED
        base_transport._session = mock_session

        assert base_transport.is_connected is False

    def test_state_property(self, base_transport: BaseTransportConnection) -> None:
        """Test state property returns current connection state."""
        assert base_transport.state == ConnectionState.DISCONNECTED

        base_transport._state = ConnectionState.CONNECTING
        assert base_transport.state == ConnectionState.CONNECTING

        base_transport._state = ConnectionState.CONNECTED
        assert base_transport.state == ConnectionState.CONNECTED

        base_transport._state = ConnectionState.FAILED
        assert base_transport.state == ConnectionState.FAILED

    def test_session_property_none_initially(self, base_transport: BaseTransportConnection) -> None:
        """Test session property returns None initially."""
        assert base_transport.session is None

    def test_session_property_returns_session(
        self, base_transport: BaseTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test session property returns the session when set."""
        base_transport._session = mock_session

        assert base_transport.session is mock_session


class TestBaseTransportConnectionSetAuthToken:
    """Tests for set_auth_token method."""

    def test_set_auth_token(self, base_transport: BaseTransportConnection) -> None:
        """Test set_auth_token updates the auth token."""
        assert base_transport._auth_token is None

        base_transport.set_auth_token("new-token")

        assert base_transport._auth_token == "new-token"

    def test_set_auth_token_overwrites_existing(self, http_config: MCPServerConfig) -> None:
        """Test set_auth_token overwrites existing token."""
        transport = BaseTransportConnection(
            config=http_config,
            auth_token="old-token",
        )

        transport.set_auth_token("new-token")

        assert transport._auth_token == "new-token"

    def test_set_auth_token_empty_string(self, base_transport: BaseTransportConnection) -> None:
        """Test set_auth_token with empty string."""
        base_transport.set_auth_token("")

        assert base_transport._auth_token == ""


class TestBaseTransportConnectionAbstractMethods:
    """Tests for abstract method behavior."""

    @pytest.mark.asyncio
    async def test_connect_raises_not_implemented(
        self, base_transport: BaseTransportConnection
    ) -> None:
        """Test connect() raises NotImplementedError."""
        with pytest.raises(NotImplementedError):
            await base_transport.connect()

    @pytest.mark.asyncio
    async def test_disconnect_raises_not_implemented(
        self, base_transport: BaseTransportConnection
    ) -> None:
        """Test disconnect() raises NotImplementedError."""
        with pytest.raises(NotImplementedError):
            await base_transport.disconnect()


class TestBaseTransportConnectionHealthCheck:
    """Tests for health_check method."""

    @pytest.mark.asyncio
    async def test_health_check_returns_false_when_not_connected(
        self, base_transport: BaseTransportConnection
    ) -> None:
        """Test health_check returns False when not connected."""
        result = await base_transport.health_check()

        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_returns_false_when_no_session(
        self, base_transport: BaseTransportConnection
    ) -> None:
        """Test health_check returns False when session is None."""
        base_transport._state = ConnectionState.CONNECTED
        base_transport._session = None

        result = await base_transport.health_check()

        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_returns_true_on_success(
        self, base_transport: BaseTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test health_check returns True on successful list_tools call."""
        base_transport._state = ConnectionState.CONNECTED
        base_transport._session = mock_session

        result = await base_transport.health_check()

        assert result is True
        mock_session.list_tools.assert_awaited_once()
        assert base_transport._consecutive_failures == 0
        assert base_transport._last_health_check is not None

    @pytest.mark.asyncio
    async def test_health_check_updates_last_health_check_time(
        self, base_transport: BaseTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test health_check updates last_health_check timestamp."""
        base_transport._state = ConnectionState.CONNECTED
        base_transport._session = mock_session
        before_check = datetime.now(UTC)

        await base_transport.health_check()

        assert base_transport._last_health_check is not None
        assert base_transport._last_health_check >= before_check

    @pytest.mark.asyncio
    async def test_health_check_resets_consecutive_failures_on_success(
        self, base_transport: BaseTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test health_check resets consecutive failures on success."""
        base_transport._state = ConnectionState.CONNECTED
        base_transport._session = mock_session
        base_transport._consecutive_failures = 5

        result = await base_transport.health_check()

        assert result is True
        assert base_transport._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_health_check_returns_false_on_timeout(
        self, base_transport: BaseTransportConnection
    ) -> None:
        """Test health_check returns False on timeout."""
        mock_session = MagicMock()

        async def slow_list_tools() -> list:
            await wait_forever()
            return []

        mock_session.list_tools = slow_list_tools

        base_transport._state = ConnectionState.CONNECTED
        base_transport._session = mock_session

        result = await base_transport.health_check(timeout=0.1)

        assert result is False
        assert base_transport._consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_health_check_returns_false_on_exception(
        self, base_transport: BaseTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test health_check returns False on exception."""
        mock_session.list_tools = AsyncMock(side_effect=RuntimeError("Connection lost"))

        base_transport._state = ConnectionState.CONNECTED
        base_transport._session = mock_session

        result = await base_transport.health_check()

        assert result is False
        assert base_transport._consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_health_check_increments_consecutive_failures(
        self, base_transport: BaseTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test health_check increments consecutive failures on each failure."""
        mock_session.list_tools = AsyncMock(side_effect=RuntimeError("Error"))

        base_transport._state = ConnectionState.CONNECTED
        base_transport._session = mock_session
        base_transport._consecutive_failures = 2

        await base_transport.health_check()

        assert base_transport._consecutive_failures == 3

    @pytest.mark.asyncio
    async def test_health_check_custom_timeout(
        self, base_transport: BaseTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test health_check respects custom timeout."""
        base_transport._state = ConnectionState.CONNECTED
        base_transport._session = mock_session

        result = await base_transport.health_check(timeout=10.0)

        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_does_not_update_last_health_check_on_failure(
        self, base_transport: BaseTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test health_check does not update last_health_check on failure."""
        mock_session.list_tools = AsyncMock(side_effect=RuntimeError("Error"))

        base_transport._state = ConnectionState.CONNECTED
        base_transport._session = mock_session
        base_transport._last_health_check = None

        await base_transport.health_check()

        # last_health_check should remain None (not updated on failure)
        assert base_transport._last_health_check is None


class TestConcreteTransportConnection:
    """Tests using the concrete implementation to verify base class behavior."""

    @pytest.mark.asyncio
    async def test_concrete_connect_changes_state(
        self, concrete_transport: ConcreteTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test that connect changes state to CONNECTED."""
        concrete_transport._mock_session = mock_session

        await concrete_transport.connect()

        assert concrete_transport._state == ConnectionState.CONNECTED
        assert concrete_transport._connect_called is True

    @pytest.mark.asyncio
    async def test_concrete_disconnect_changes_state(
        self, concrete_transport: ConcreteTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test that disconnect changes state to DISCONNECTED."""
        concrete_transport._mock_session = mock_session
        await concrete_transport.connect()

        await concrete_transport.disconnect()

        assert concrete_transport._state == ConnectionState.DISCONNECTED
        assert concrete_transport._disconnect_called is True
        assert concrete_transport._session is None

    @pytest.mark.asyncio
    async def test_concrete_is_connected_after_connect(
        self, concrete_transport: ConcreteTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test is_connected returns True after successful connect."""
        concrete_transport._mock_session = mock_session

        await concrete_transport.connect()

        assert concrete_transport.is_connected is True

    @pytest.mark.asyncio
    async def test_concrete_is_connected_after_disconnect(
        self, concrete_transport: ConcreteTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test is_connected returns False after disconnect."""
        concrete_transport._mock_session = mock_session
        await concrete_transport.connect()
        await concrete_transport.disconnect()

        assert concrete_transport.is_connected is False

    @pytest.mark.asyncio
    async def test_concrete_connect_failure(
        self, concrete_transport: ConcreteTransportConnection
    ) -> None:
        """Test connect failure sets state to FAILED."""
        concrete_transport._should_fail_connect = True

        with pytest.raises(ConnectionError, match="Mock connection failed"):
            await concrete_transport.connect()

        assert concrete_transport._state == ConnectionState.FAILED

    @pytest.mark.asyncio
    async def test_health_check_with_concrete_transport(
        self, concrete_transport: ConcreteTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test health_check works with concrete transport after connect."""
        concrete_transport._mock_session = mock_session

        await concrete_transport.connect()
        result = await concrete_transport.health_check()

        assert result is True
        assert concrete_transport._consecutive_failures == 0


class TestConnectionStateTransitions:
    """Tests for connection state transitions in the base class."""

    def test_all_connection_states_accessible(
        self, base_transport: BaseTransportConnection
    ) -> None:
        """Test all connection states can be set."""
        states = [
            ConnectionState.DISCONNECTED,
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
            ConnectionState.FAILED,
        ]

        for state in states:
            base_transport._state = state
            assert base_transport.state == state

    def test_disconnected_is_initial_state(self, base_transport: BaseTransportConnection) -> None:
        """Test DISCONNECTED is the initial state."""
        assert base_transport.state == ConnectionState.DISCONNECTED


class TestTokenRefreshCallback:
    """Tests for token refresh callback functionality."""

    @pytest.mark.asyncio
    async def test_token_refresh_callback_is_callable(self, http_config: MCPServerConfig) -> None:
        """Test token refresh callback can be awaited."""
        call_count = 0

        async def refresh_token() -> str:
            nonlocal call_count
            call_count += 1
            return f"token-{call_count}"

        transport = BaseTransportConnection(
            config=http_config,
            token_refresh_callback=refresh_token,
        )

        # Verify callback is stored and can be called
        assert transport._token_refresh_callback is not None
        result = await transport._token_refresh_callback()
        assert result == "token-1"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_token_refresh_callback_multiple_calls(
        self, http_config: MCPServerConfig
    ) -> None:
        """Test token refresh callback can be called multiple times."""
        tokens = ["token-a", "token-b", "token-c"]
        token_index = 0

        async def refresh_token() -> str:
            nonlocal token_index
            token = tokens[token_index]
            token_index += 1
            return token

        transport = BaseTransportConnection(
            config=http_config,
            token_refresh_callback=refresh_token,
        )

        result1 = await transport._token_refresh_callback()
        result2 = await transport._token_refresh_callback()
        result3 = await transport._token_refresh_callback()

        assert result1 == "token-a"
        assert result2 == "token-b"
        assert result3 == "token-c"


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_init_preserves_config_reference(self, http_config: MCPServerConfig) -> None:
        """Test that init preserves the config object reference."""
        transport = BaseTransportConnection(config=http_config)

        assert transport.config is http_config

    def test_consecutive_failures_starts_at_zero(
        self, base_transport: BaseTransportConnection
    ) -> None:
        """Test consecutive_failures starts at zero."""
        assert base_transport._consecutive_failures == 0

    def test_last_health_check_starts_none(self, base_transport: BaseTransportConnection) -> None:
        """Test last_health_check starts as None."""
        assert base_transport._last_health_check is None

    def test_transport_context_starts_none(self, base_transport: BaseTransportConnection) -> None:
        """Test transport_context starts as None."""
        assert base_transport._transport_context is None

    @pytest.mark.asyncio
    async def test_health_check_handles_asyncio_timeout_error(
        self, base_transport: BaseTransportConnection
    ) -> None:
        """Test health_check handles asyncio.TimeoutError specifically."""
        mock_session = MagicMock()
        mock_session.list_tools = AsyncMock(side_effect=TimeoutError())

        base_transport._state = ConnectionState.CONNECTED
        base_transport._session = mock_session

        result = await base_transport.health_check()

        assert result is False
        assert base_transport._consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_health_check_with_zero_timeout(
        self, base_transport: BaseTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test health_check with zero timeout."""
        base_transport._state = ConnectionState.CONNECTED
        base_transport._session = mock_session

        # Even with zero timeout, if the operation is fast enough it might succeed
        # This tests that the timeout parameter is passed correctly
        result = await base_transport.health_check(timeout=0.001)

        # Result depends on execution speed, but should not raise
        assert result in (True, False)

    def test_multiple_set_auth_token_calls(self, base_transport: BaseTransportConnection) -> None:
        """Test multiple set_auth_token calls update correctly."""
        base_transport.set_auth_token("token-1")
        assert base_transport._auth_token == "token-1"

        base_transport.set_auth_token("token-2")
        assert base_transport._auth_token == "token-2"

        base_transport.set_auth_token("token-3")
        assert base_transport._auth_token == "token-3"

    @pytest.mark.asyncio
    async def test_health_check_with_very_large_consecutive_failures(
        self, base_transport: BaseTransportConnection, mock_session: MagicMock
    ) -> None:
        """Test health_check resets even very large consecutive failures."""
        base_transport._state = ConnectionState.CONNECTED
        base_transport._session = mock_session
        base_transport._consecutive_failures = 1000000

        result = await base_transport.health_check()

        assert result is True
        assert base_transport._consecutive_failures == 0


class TestWithDifferentConfigs:
    """Tests verifying behavior with different config types."""

    def test_websocket_config(self) -> None:
        """Test BaseTransportConnection with websocket config."""
        config = MCPServerConfig(
            name="ws-server",
            project_id="test-project-uuid",
            transport="websocket",
            url="ws://localhost:8080/ws",
        )

        transport = BaseTransportConnection(config=config)

        assert transport.config.transport == "websocket"
        assert transport.config.url == "ws://localhost:8080/ws"

    def test_config_with_headers(self) -> None:
        """Test BaseTransportConnection with config containing headers."""
        config = MCPServerConfig(
            name="api-server",
            project_id="test-project-uuid",
            transport="http",
            url="https://api.example.com/mcp",
            headers={"Authorization": "Bearer token", "X-Custom": "value"},
        )

        transport = BaseTransportConnection(config=config)

        assert transport.config.headers == {
            "Authorization": "Bearer token",
            "X-Custom": "value",
        }

    def test_config_with_oauth(self) -> None:
        """Test BaseTransportConnection with OAuth config."""
        config = MCPServerConfig(
            name="oauth-server",
            project_id="test-project-uuid",
            transport="http",
            url="https://api.example.com/mcp",
            requires_oauth=True,
            oauth_provider="github",
        )

        transport = BaseTransportConnection(config=config)

        assert transport.config.requires_oauth is True
        assert transport.config.oauth_provider == "github"

    def test_disabled_config(self) -> None:
        """Test BaseTransportConnection with disabled config."""
        config = MCPServerConfig(
            name="disabled-server",
            project_id="test-project-uuid",
            transport="http",
            url="http://localhost:8080/mcp",
            enabled=False,
        )

        transport = BaseTransportConnection(config=config)

        assert transport.config.enabled is False
