"""Tests for ServerManagementService."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.services.server_mgmt import ServerManagementService

pytestmark = pytest.mark.unit


class TestServerManagementServiceImport:
    """Tests for ServerManagementService.import_server()."""

    @pytest.fixture
    def mock_mcp_manager(self):
        """Create a mock MCP manager."""
        return MagicMock()

    @pytest.fixture
    def mock_config(self):
        """Create a mock daemon config."""
        config = MagicMock()
        import_config = MagicMock()
        import_config.enabled = True
        import_config.prompt = "test prompt"
        import_config.model = "test-model"
        config.get_import_mcp_server_config.return_value = import_config
        return config

    @pytest.fixture
    def service(self, mock_mcp_manager, mock_config):
        """Create a ServerManagementService instance."""
        return ServerManagementService(
            mcp_manager=mock_mcp_manager,
            config_manager=MagicMock(),
            config=mock_config,
        )

    @pytest.fixture
    def service_no_config(self, mock_mcp_manager):
        """Create a ServerManagementService without config."""
        return ServerManagementService(
            mcp_manager=mock_mcp_manager,
            config_manager=MagicMock(),
            config=None,
        )

    async def test_import_requires_source(self, service):
        """Test that import_server requires at least one source."""
        result = await service.import_server()

        assert result["success"] is False
        assert "Specify at least one" in result["error"]

    async def test_import_without_config_fails(self, service_no_config):
        """Test that import fails without daemon config."""
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "test-project"},
        ):
            result = await service_no_config.import_server(from_project="other-project")

        assert result["success"] is False
        assert "configuration not available" in result["error"]

    async def test_import_without_project_context_fails(self, service):
        """Test that import fails without project context."""
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value=None,
        ):
            result = await service.import_server(from_project="other-project")

        assert result["success"] is False
        assert "No current project" in result["error"]

    async def test_import_from_project_delegates_to_importer(self, service):
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
                "gobby.storage.database.LocalDatabase",
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

    async def test_import_from_github_delegates_to_importer(self, service):
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
                "gobby.storage.database.LocalDatabase",
            ),
        ):
            result = await service.import_server(
                github_url="https://github.com/test/repo",
            )

        assert result["success"] is True
        mock_importer.import_from_github.assert_called_once_with("https://github.com/test/repo")
        assert mock_importer.import_from_github.call_count == 1
        assert mock_importer.import_from_github.call_args is not None

    async def test_import_from_query_delegates_to_importer(self, service):
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
                "gobby.storage.database.LocalDatabase",
            ),
        ):
            result = await service.import_server(query="supabase mcp server")

        assert result["success"] is True
        mock_importer.import_from_query.assert_called_once_with("supabase mcp server")
        assert mock_importer.import_from_query.call_count == 1
        assert mock_importer.import_from_query.call_args is not None

    async def test_import_handles_exception(self, service):
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
                "gobby.storage.database.LocalDatabase",
            ),
        ):
            result = await service.import_server(from_project="test")

        assert result["success"] is False
        assert "Connection failed" in result["error"]

    async def test_import_priority_from_project_first(self, service):
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
                "gobby.storage.database.LocalDatabase",
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
