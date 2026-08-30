"""Tests for ServerManagementService."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.services.server_mgmt import ServerManagementService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp import LocalMCPManager

pytestmark = pytest.mark.unit


class TestServerManagementServicePersistence:
    """Tests durable add/remove behavior through MCPClientManager."""

    async def test_add_and_remove_survive_manager_restarts(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, Any],
    ) -> None:
        project_id = sample_project["id"]
        storage = LocalMCPManager(temp_db)
        manager = MCPClientManager(
            server_configs=[],
            project_id=project_id,
            mcp_db_manager=storage,
        )
        service = ServerManagementService(manager, config_manager=MagicMock())

        added = await service.add_server(
            "durable-server",
            "http",
            url="https://mcp.example.test",
            enabled=False,
            project_id=project_id,
        )

        assert added["success"] is True
        stored = storage.get_server("durable-server", project_id)
        assert stored is not None
        restarted = MCPClientManager(
            project_id=project_id,
            mcp_db_manager=storage,
        )
        assert restarted.has_server(stored.id)

        removed = await ServerManagementService(
            restarted,
            config_manager=MagicMock(),
        ).remove_server("durable-server")

        assert removed["success"] is True
        assert storage.get_server("durable-server", project_id) is None
        restarted_again = MCPClientManager(
            project_id=project_id,
            mcp_db_manager=storage,
        )
        assert not restarted_again.has_server("durable-server")

    async def test_duplicate_add_raises_value_error(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, Any],
    ) -> None:
        project_id = sample_project["id"]
        storage = LocalMCPManager(temp_db)
        manager = MCPClientManager(
            server_configs=[],
            project_id=project_id,
            mcp_db_manager=storage,
        )
        service = ServerManagementService(manager, config_manager=MagicMock())
        kwargs = {
            "url": "https://mcp.example.test",
            "enabled": False,
            "project_id": project_id,
        }
        await service.add_server("duplicate-server", "http", **kwargs)

        duplicate = await service.add_server("duplicate-server", "http", **kwargs)
        assert duplicate["success"] is False
        assert duplicate["error"] == "duplicate"

    async def test_add_rolls_back_runtime_state_when_persistence_fails(
        self,
        sample_project: dict[str, Any],
    ) -> None:
        project_id = sample_project["id"]
        storage = MagicMock()
        storage.insert_server.side_effect = RuntimeError("database unavailable")
        storage.get_template.return_value = None
        manager = MCPClientManager(
            server_configs=[],
            project_id=project_id,
            mcp_db_manager=storage,
        )
        service = ServerManagementService(manager, config_manager=MagicMock())

        result = await service.add_server(
            "ephemeral-server",
            "http",
            url="https://mcp.example.test",
            enabled=False,
            project_id=project_id,
        )

        assert result == {"success": False, "error": "database unavailable"}
        assert not manager.has_server("ephemeral-server")
        assert "ephemeral-server" not in manager.get_lazy_connection_states()


class TestServerManagementServiceConnectionStatus:
    """Tests that the agent-facing service preserves manager connection status."""

    @pytest.mark.parametrize(
        ("manager_result", "expected_message"),
        [
            (
                {"success": True, "connected": True, "full_tool_schemas": []},
                "Server test-server added successfully",
            ),
            (
                {"success": True, "connected": False, "full_tool_schemas": []},
                "Server test-server added successfully",
            ),
            (
                {
                    "success": True,
                    "connected": False,
                    "error": "connection refused",
                    "full_tool_schemas": [],
                },
                "Server test-server added but connection failed",
            ),
        ],
    )
    async def test_add_server_preserves_manager_connection_status(
        self,
        manager_result: dict[str, object],
        expected_message: str,
    ) -> None:
        manager = MagicMock()

        async def add_server(_config: object) -> dict[str, object]:
            return manager_result

        manager.add_server = add_server
        service = ServerManagementService(manager, config_manager=MagicMock())

        result = await service.add_server(
            "test-server",
            "http",
            url="https://mcp.example.test",
            enabled=bool(manager_result["connected"]),
            project_id="11111111-1111-4111-8111-111111111111",
        )

        assert result["success"] is True
        assert result["connected"] is manager_result["connected"]
        assert result["message"] == expected_message
        if "error" in manager_result:
            assert result["error"] == manager_result["error"]
        else:
            assert "error" not in result


class TestServerManagementServiceImport:
    """Tests for ServerManagementService.import_server()."""

    @pytest.fixture
    def mock_mcp_manager(self) -> MagicMock:
        """Create a mock MCP manager."""
        return MagicMock()

    @pytest.fixture
    def mock_config(self) -> MagicMock:
        """Create a mock daemon config."""
        config = MagicMock()
        import_config = MagicMock()
        import_config.enabled = True
        import_config.prompt = "test prompt"
        import_config.model = "test-model"
        config.get_import_mcp_server_config.return_value = import_config
        return config

    @pytest.fixture
    def service(
        self,
        mock_mcp_manager: MagicMock,
        mock_config: MagicMock,
    ) -> ServerManagementService:
        """Create a ServerManagementService instance."""
        return ServerManagementService(
            mcp_manager=mock_mcp_manager,
            config_manager=MagicMock(),
            config_resolver=lambda: mock_config,
        )

    @pytest.fixture
    def service_no_config(self, mock_mcp_manager: MagicMock) -> ServerManagementService:
        """Create a ServerManagementService without config."""
        return ServerManagementService(
            mcp_manager=mock_mcp_manager,
            config_manager=MagicMock(),
            config_resolver=lambda: None,
        )

    async def test_import_requires_source(self, service: ServerManagementService) -> None:
        """Test that import_server requires at least one source."""
        result = await service.import_server()

        assert result["success"] is False
        assert "Specify at least one" in result["error"]

    async def test_import_without_config_fails(
        self, service_no_config: ServerManagementService
    ) -> None:
        """Test that import fails without daemon config."""
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "test-project"},
        ):
            result = await service_no_config.import_server(from_project="other-project")

        assert result["success"] is False
        assert "configuration not available" in result["error"]

    async def test_import_without_project_context_fails(
        self, service: ServerManagementService
    ) -> None:
        """Test that import fails without project context."""
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value=None,
        ):
            result = await service.import_server(from_project="other-project")

        assert result["success"] is False
        assert "No current project" in result["error"]

    async def test_import_from_project_delegates_to_importer(
        self, service: ServerManagementService
    ) -> None:
        """Test that from_project delegates to MCPServerImporter.import_from_project."""
        mock_importer = MagicMock()
        mock_importer.import_from_project = AsyncMock(
            return_value={"success": True, "imported": ["server1"]}
        )

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "test-project"},
            ),
            patch(
                "gobby.mcp_proxy.importer.MCPServerImporter",
                return_value=mock_importer,
            ),
            patch(
                "gobby.storage.hub.protocol.HubDatabase",
            ),
        ):
            result = await service.import_server(
                from_project="source-project",
                servers=["server1"],
            )

        assert result["success"] is True
        assert result["imported"] == ["server1"]
        mock_importer.import_from_project.assert_called_once_with(
            source_project="source-project",
            servers=["server1"],
        )

    async def test_import_from_github_delegates_to_importer(
        self, service: ServerManagementService
    ) -> None:
        """Test that github_url delegates to MCPServerImporter.import_from_github."""
        mock_importer = MagicMock()
        mock_importer.import_from_github = AsyncMock(
            return_value={"success": True, "imported": ["github-server"]}
        )

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "test-project"},
            ),
            patch(
                "gobby.mcp_proxy.importer.MCPServerImporter",
                return_value=mock_importer,
            ),
            patch(
                "gobby.storage.hub.protocol.HubDatabase",
            ),
        ):
            result = await service.import_server(
                github_url="https://github.com/test/repo",
            )

        assert result["success"] is True
        mock_importer.import_from_github.assert_called_once_with("https://github.com/test/repo")
        assert mock_importer.import_from_github.call_count == 1
        assert mock_importer.import_from_github.call_args is not None

    async def test_import_from_query_delegates_to_importer(
        self, service: ServerManagementService
    ) -> None:
        """Test that query delegates to MCPServerImporter.import_from_query."""
        mock_importer = MagicMock()
        mock_importer.import_from_query = AsyncMock(
            return_value={"success": True, "imported": ["searched-server"]}
        )

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "test-project"},
            ),
            patch(
                "gobby.mcp_proxy.importer.MCPServerImporter",
                return_value=mock_importer,
            ),
            patch(
                "gobby.storage.hub.protocol.HubDatabase",
            ),
        ):
            result = await service.import_server(query="supabase mcp server")

        assert result["success"] is True
        mock_importer.import_from_query.assert_called_once_with("supabase mcp server")
        assert mock_importer.import_from_query.call_count == 1
        assert mock_importer.import_from_query.call_args is not None

    async def test_import_handles_exception(self, service: ServerManagementService) -> None:
        """Test that exceptions are caught and returned as errors."""
        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "test-project"},
            ),
            patch(
                "gobby.mcp_proxy.importer.MCPServerImporter",
                side_effect=Exception("Connection failed"),
            ),
            patch(
                "gobby.storage.hub.protocol.HubDatabase",
            ),
        ):
            result = await service.import_server(from_project="test")

        assert result["success"] is False
        assert "Connection failed" in result["error"]

    async def test_import_priority_from_project_first(
        self, service: ServerManagementService
    ) -> None:
        """Test that from_project takes priority when multiple sources provided."""
        mock_importer = MagicMock()
        mock_importer.import_from_project = AsyncMock(
            return_value={"success": True, "imported": ["project-server"]}
        )
        mock_importer.import_from_github = AsyncMock()
        mock_importer.import_from_query = AsyncMock()

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "test-project"},
            ),
            patch(
                "gobby.mcp_proxy.importer.MCPServerImporter",
                return_value=mock_importer,
            ),
            patch(
                "gobby.storage.hub.protocol.HubDatabase",
            ),
        ):
            await service.import_server(
                from_project="source",
                github_url="https://github.com/test/repo",
                query="test query",
            )

        # from_project should be used, others ignored
        mock_importer.import_from_project.assert_called_once()
        assert mock_importer.import_from_project.call_count == 1
        assert mock_importer.import_from_project.call_args is not None
        mock_importer.import_from_github.assert_not_called()
        assert mock_importer.import_from_github.call_count == 0
        assert not mock_importer.import_from_github.called
        mock_importer.import_from_query.assert_not_called()
        assert mock_importer.import_from_query.call_count == 0
        assert not mock_importer.import_from_query.called


async def test_add_disabled_template_returns_template_disabled_without_persisting(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    project_id = sample_project["id"]
    storage = LocalMCPManager(temp_db)
    storage.upsert_template(
        name="disabled-tmpl",
        project_id=project_id,
        owner="user",
        definition={
            "name": "disabled-tmpl",
            "description": "Disabled",
            "version": 1,
            "transport": "stdio",
            "command": "uvx",
            "args": ["demo"],
            "enabled": False,
        },
        enabled=False,
    )
    manager = MCPClientManager(
        server_configs=[],
        project_id=project_id,
        mcp_db_manager=storage,
        lazy_connect=True,
    )
    service = ServerManagementService(manager, config_manager=MagicMock())

    result = await service.add_server(
        "disabled-instance",
        template="disabled-tmpl",
        values={},
        scope="project",
        project_id=project_id,
    )

    assert result["success"] is False
    assert result["error"] == "template_disabled"
    assert storage.get_server("disabled-instance", project_id) is None
    assert manager.server_configs == []


async def test_concurrent_add_same_name_and_scope_has_one_winner(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    import asyncio

    project_id = sample_project["id"]
    storage = LocalMCPManager(temp_db)
    manager = MCPClientManager(
        server_configs=[],
        project_id=project_id,
        mcp_db_manager=storage,
        lazy_connect=True,
    )
    service = ServerManagementService(manager, config_manager=MagicMock())

    async def add(env: dict[str, str]) -> dict[str, Any]:
        return await service.add_server(
            "shared-name",
            "http",
            url="https://mcp.example.test",
            env=env,
            enabled=False,
            project_id=project_id,
        )

    first, second = await asyncio.gather(
        add({"TOKEN": "winner-token", "WINNER_ONLY": "keep"}),
        add({"TOKEN": "loser-token", "LOSER_ONLY": "drop"}),
    )
    results = [first, second]
    winners = [item for item in results if item.get("success") is True]
    losers = [item for item in results if item.get("error") == "duplicate"]
    assert len(winners) == 1
    assert len(losers) == 1
    rows = temp_db.fetchall(
        "SELECT id, name FROM mcp_servers WHERE name = %s AND project_id = %s",
        ("shared-name", project_id),
    )
    assert len(rows) == 1
    assert len(manager.server_configs) == 1
    stored = storage.get_server("shared-name", project_id)
    assert stored is not None
    stored_env = stored.env or {}
    assert ("WINNER_ONLY" in stored_env) != ("LOSER_ONLY" in stored_env)
    loser_token = "loser-token" if "WINNER_ONLY" in stored_env else "winner-token"
    assert loser_token not in str(stored_env)
