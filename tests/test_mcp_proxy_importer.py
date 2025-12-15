"""Tests for the MCP proxy importer module."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.app import DaemonConfig, ImportMCPServerConfig
from gobby.mcp_proxy.importer import MCPServerImporter, SECRET_PLACEHOLDER_PATTERN


@pytest.fixture
def mock_config() -> DaemonConfig:
    """Create a mock DaemonConfig."""
    config = MagicMock(spec=DaemonConfig)
    config.get_import_mcp_server_config.return_value = ImportMCPServerConfig(
        enabled=True,
        model="claude-haiku-4-5",
        prompt="Extract MCP config",
    )
    return config


@pytest.fixture
def mock_config_disabled() -> DaemonConfig:
    """Create a mock DaemonConfig with import disabled."""
    config = MagicMock(spec=DaemonConfig)
    config.get_import_mcp_server_config.return_value = ImportMCPServerConfig(
        enabled=False,
        model="claude-haiku-4-5",
        prompt="",
    )
    return config


@pytest.fixture
def mock_db():
    """Create a mock database."""
    return MagicMock()


@pytest.fixture
def importer(mock_config, mock_db):
    """Create an MCPServerImporter instance."""
    with patch("gobby.mcp_proxy.importer.LocalMCPManager") as mock_mcp_manager_cls:
        with patch("gobby.mcp_proxy.importer.LocalProjectManager") as mock_project_manager_cls:
            mock_mcp_manager = MagicMock()
            mock_project_manager = MagicMock()
            mock_mcp_manager_cls.return_value = mock_mcp_manager
            mock_project_manager_cls.return_value = mock_project_manager

            importer = MCPServerImporter(
                config=mock_config,
                db=mock_db,
                current_project_id="test-project-id",
            )
            # Store the mocks for tests to access
            importer.mcp_db_manager = mock_mcp_manager
            importer.project_manager = mock_project_manager
            yield importer


class TestSecretPlaceholderPattern:
    """Tests for SECRET_PLACEHOLDER_PATTERN regex."""

    def test_matches_simple_placeholder(self):
        """Test matches simple API key placeholder."""
        text = "<YOUR_API_KEY>"
        match = SECRET_PLACEHOLDER_PATTERN.search(text)
        assert match is not None
        assert match.group(0) == "<YOUR_API_KEY>"

    def test_matches_complex_placeholder(self):
        """Test matches complex placeholder names."""
        text = "<YOUR_OPENAI_API_KEY>"
        match = SECRET_PLACEHOLDER_PATTERN.search(text)
        assert match is not None

    def test_matches_with_numbers(self):
        """Test matches placeholders with numbers."""
        text = "<YOUR_API_KEY_V2>"
        match = SECRET_PLACEHOLDER_PATTERN.search(text)
        assert match is not None

    def test_does_not_match_lowercase(self):
        """Test does not match lowercase placeholders."""
        text = "<your_api_key>"
        match = SECRET_PLACEHOLDER_PATTERN.search(text)
        assert match is None

    def test_does_not_match_regular_text(self):
        """Test does not match regular text."""
        text = "sk-1234567890"
        match = SECRET_PLACEHOLDER_PATTERN.search(text)
        assert match is None


class TestMCPServerImporterInit:
    """Tests for MCPServerImporter initialization."""

    def test_init_stores_config(self, mock_config, mock_db):
        """Test initialization stores configuration."""
        importer = MCPServerImporter(
            config=mock_config,
            db=mock_db,
            current_project_id="project-123",
        )

        assert importer.config == mock_config
        assert importer.db == mock_db
        assert importer.current_project_id == "project-123"

    def test_init_with_mcp_manager(self, mock_config, mock_db):
        """Test initialization with MCP client manager."""
        mock_manager = MagicMock()

        importer = MCPServerImporter(
            config=mock_config,
            db=mock_db,
            current_project_id="project-123",
            mcp_client_manager=mock_manager,
        )

        assert importer.mcp_client_manager == mock_manager


class TestExtractJson:
    """Tests for _extract_json method."""

    def test_extracts_json_from_code_block(self, importer):
        """Test extracts JSON from markdown code block."""
        text = """
Here is the config:
```json
{"name": "test-server", "transport": "http"}
```
"""
        result = importer._extract_json(text)

        assert result is not None
        assert result["name"] == "test-server"
        assert result["transport"] == "http"

    def test_extracts_json_from_code_block_no_language(self, importer):
        """Test extracts JSON from code block without language."""
        text = """
```
{"name": "server", "transport": "stdio"}
```
"""
        result = importer._extract_json(text)

        assert result is not None
        assert result["name"] == "server"

    def test_extracts_raw_json(self, importer):
        """Test extracts raw JSON from text."""
        text = 'The config is {"name": "raw", "transport": "http"} here.'

        result = importer._extract_json(text)

        assert result is not None
        assert result["name"] == "raw"

    def test_returns_none_for_invalid_json(self, importer):
        """Test returns None for invalid JSON."""
        text = "This is not JSON at all"

        result = importer._extract_json(text)
        assert result is None

    def test_prefers_valid_server_config(self, importer):
        """Test prefers JSON that looks like server config."""
        text = """
{"unrelated": "data"}
{"name": "real-server", "transport": "http"}
"""
        result = importer._extract_json(text)

        # Should find the one with name/transport
        assert result is not None
        assert result.get("name") == "real-server"


class TestFindMissingSecrets:
    """Tests for _find_missing_secrets method."""

    def test_finds_placeholder_in_string(self, importer):
        """Test finds placeholder in simple string."""
        config = {"api_key": "<YOUR_API_KEY>"}

        result = importer._find_missing_secrets(config)

        assert "<YOUR_API_KEY>" in result

    def test_finds_placeholder_in_nested_dict(self, importer):
        """Test finds placeholder in nested dict."""
        config = {
            "name": "server",
            "headers": {
                "Authorization": "Bearer <YOUR_TOKEN>"
            }
        }

        result = importer._find_missing_secrets(config)

        assert "<YOUR_TOKEN>" in result

    def test_finds_placeholder_in_list(self, importer):
        """Test finds placeholder in list."""
        config = {
            "args": ["--key", "<YOUR_SECRET_KEY>"]
        }

        result = importer._find_missing_secrets(config)

        assert "<YOUR_SECRET_KEY>" in result

    def test_returns_empty_for_no_placeholders(self, importer):
        """Test returns empty list when no placeholders."""
        config = {
            "name": "server",
            "url": "http://localhost:8080",
        }

        result = importer._find_missing_secrets(config)

        assert result == []

    def test_finds_multiple_placeholders(self, importer):
        """Test finds multiple placeholders."""
        config = {
            "headers": {
                "X-Api-Key": "<YOUR_API_KEY>",
                "X-Secret": "<YOUR_SECRET>",
            }
        }

        result = importer._find_missing_secrets(config)

        assert len(result) == 2
        assert "<YOUR_API_KEY>" in result
        assert "<YOUR_SECRET>" in result


class TestImportFromProject:
    """Tests for import_from_project method."""

    @pytest.mark.asyncio
    async def test_returns_error_for_unknown_project(self, importer):
        """Test returns error when project not found."""
        with patch.object(importer.project_manager, "get_by_name", return_value=None):
            with patch.object(importer.project_manager, "get", return_value=None):
                with patch.object(importer.project_manager, "list", return_value=[]):
                    result = await importer.import_from_project("unknown-project")

                    assert result["success"] is False
                    assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_returns_error_for_empty_project(self, importer):
        """Test returns error when project has no servers."""
        mock_project = MagicMock()
        mock_project.id = "proj-123"
        mock_project.name = "empty-project"

        with patch.object(importer.project_manager, "get_by_name", return_value=mock_project):
            with patch.object(importer.mcp_db_manager, "list_servers", return_value=[]):
                result = await importer.import_from_project("empty-project")

                assert result["success"] is False
                assert "No MCP servers found" in result["error"]

    @pytest.mark.asyncio
    async def test_imports_servers_successfully(self, importer):
        """Test successfully imports servers."""
        mock_project = MagicMock()
        mock_project.id = "source-proj"
        mock_project.name = "source-project"

        mock_server = MagicMock()
        mock_server.name = "test-server"
        mock_server.transport = "http"
        mock_server.url = "http://localhost:8080"
        mock_server.command = None
        mock_server.args = None
        mock_server.env = None
        mock_server.headers = None
        mock_server.enabled = True
        mock_server.description = "Test server"

        with patch.object(importer.project_manager, "get_by_name", return_value=mock_project):
            with patch.object(importer.mcp_db_manager, "list_servers", side_effect=[
                [mock_server],  # Source servers
                [],  # Existing servers (none)
            ]):
                with patch.object(importer, "_add_server", new_callable=AsyncMock, return_value={"success": True}):
                    result = await importer.import_from_project("source-project")

                    assert result["success"] is True
                    assert "test-server" in result["imported"]

    @pytest.mark.asyncio
    async def test_skips_existing_servers(self, importer):
        """Test skips servers that already exist."""
        mock_project = MagicMock()
        mock_project.id = "source-proj"
        mock_project.name = "source-project"

        mock_server = MagicMock()
        mock_server.name = "existing-server"
        mock_server.transport = "http"

        existing_server = MagicMock()
        existing_server.name = "existing-server"

        with patch.object(importer.project_manager, "get_by_name", return_value=mock_project):
            with patch.object(importer.mcp_db_manager, "list_servers", side_effect=[
                [mock_server],  # Source servers
                [existing_server],  # Existing servers
            ]):
                result = await importer.import_from_project("source-project")

                assert "existing-server" in result.get("skipped", [])

    @pytest.mark.asyncio
    async def test_filters_by_server_names(self, importer):
        """Test filters by specified server names."""
        mock_project = MagicMock()
        mock_project.id = "proj"
        mock_project.name = "project"

        server1 = MagicMock()
        server1.name = "wanted-server"
        server1.transport = "http"
        server1.url = "http://localhost"
        server1.command = None
        server1.args = None
        server1.env = None
        server1.headers = None
        server1.enabled = True
        server1.description = None

        server2 = MagicMock()
        server2.name = "unwanted-server"
        server2.transport = "http"

        with patch.object(importer.project_manager, "get_by_name", return_value=mock_project):
            with patch.object(importer.mcp_db_manager, "list_servers", side_effect=[
                [server1, server2],  # Source
                [],  # Existing
            ]):
                with patch.object(importer, "_add_server", new_callable=AsyncMock, return_value={"success": True}):
                    result = await importer.import_from_project("project", servers=["wanted-server"])

                    assert "wanted-server" in result["imported"]
                    assert "unwanted-server" not in result.get("imported", [])


class TestImportFromGithub:
    """Tests for import_from_github method."""

    @pytest.mark.asyncio
    async def test_returns_error_when_disabled(self, mock_config_disabled, mock_db):
        """Test returns error when import is disabled."""
        importer = MCPServerImporter(
            config=mock_config_disabled,
            db=mock_db,
            current_project_id="proj",
        )

        result = await importer.import_from_github("https://github.com/test/repo")

        assert result["success"] is False
        assert "disabled" in result["error"]


class TestImportFromQuery:
    """Tests for import_from_query method."""

    @pytest.mark.asyncio
    async def test_returns_error_when_disabled(self, mock_config_disabled, mock_db):
        """Test returns error when import is disabled."""
        importer = MCPServerImporter(
            config=mock_config_disabled,
            db=mock_db,
            current_project_id="proj",
        )

        result = await importer.import_from_query("supabase mcp server")

        assert result["success"] is False
        assert "disabled" in result["error"]


class TestParseAndAddConfig:
    """Tests for _parse_and_add_config method."""

    @pytest.mark.asyncio
    async def test_returns_error_for_invalid_json(self, importer):
        """Test returns error when JSON cannot be extracted."""
        result = await importer._parse_and_add_config("No JSON here")

        assert result["success"] is False
        assert "Could not extract" in result["error"]

    @pytest.mark.asyncio
    async def test_returns_needs_configuration_for_secrets(self, importer):
        """Test returns needs_configuration when secrets are needed."""
        text = '{"name": "server", "transport": "http", "headers": {"key": "<YOUR_API_KEY>"}}'

        result = await importer._parse_and_add_config(text)

        assert result["status"] == "needs_configuration"
        assert "<YOUR_API_KEY>" in result["missing"]

    @pytest.mark.asyncio
    async def test_adds_server_without_secrets(self, importer):
        """Test adds server directly when no secrets needed."""
        text = '{"name": "no-secrets", "transport": "http", "url": "http://localhost"}'

        with patch.object(importer, "_add_server", new_callable=AsyncMock) as mock_add:
            mock_add.return_value = {"success": True, "imported": ["no-secrets"]}

            result = await importer._parse_and_add_config(text)

            mock_add.assert_called_once()
            call_kwargs = mock_add.call_args.kwargs
            assert call_kwargs["name"] == "no-secrets"
            assert call_kwargs["transport"] == "http"

    @pytest.mark.asyncio
    async def test_returns_error_for_missing_name(self, importer):
        """Test returns error when name is missing."""
        text = '{"transport": "http"}'

        result = await importer._parse_and_add_config(text)

        assert result["success"] is False
        assert "missing required fields" in result["error"]

    @pytest.mark.asyncio
    async def test_returns_error_for_missing_transport(self, importer):
        """Test returns error when transport is missing."""
        text = '{"name": "server"}'

        result = await importer._parse_and_add_config(text)

        assert result["success"] is False
        assert "missing required fields" in result["error"]

    @pytest.mark.asyncio
    async def test_includes_instructions_when_present(self, importer):
        """Test includes instructions in needs_configuration result."""
        text = '{"name": "s", "transport": "http", "headers": {"key": "<YOUR_KEY>"}, "instructions": "Get key from..."}'

        result = await importer._parse_and_add_config(text)

        assert result["status"] == "needs_configuration"
        assert result["instructions"] == "Get key from..."


class TestAddServer:
    """Tests for _add_server method."""

    @pytest.mark.asyncio
    async def test_falls_back_to_db_without_manager(self, importer):
        """Test falls back to DB when no manager available."""
        # importer fixture already has mocked mcp_db_manager
        result = await importer._add_server(
            name="test-server",
            transport="http",
            url="http://localhost",
        )

        # Should use mcp_db_manager.upsert
        importer.mcp_db_manager.upsert.assert_called_once()
        assert result["success"] is True
        assert "restart daemon" in result["message"]

    @pytest.mark.asyncio
    async def test_handles_add_failure(self, importer):
        """Test handles add server failure."""
        importer.mcp_db_manager.upsert.side_effect = Exception("DB error")

        result = await importer._add_server(
            name="failing",
            transport="http",
        )

        assert result["success"] is False
        assert "DB error" in result["error"]
