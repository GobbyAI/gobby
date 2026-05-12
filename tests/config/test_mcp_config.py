"""Tests for src/config/mcp.py - MCP Configuration Manager."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.config.mcp import MCPConfigManager, migrate_legacy_mcp_config
from gobby.mcp_proxy.manager import MCPServerConfig

pytestmark = pytest.mark.unit


class TestMCPConfigManagerInit:
    """Tests for MCPConfigManager initialization."""

    def test_init_creates_config_file_if_not_exists(self, tmp_path) -> None:
        """Test that init creates config file with empty servers if it doesn't exist."""
        config_path = tmp_path / "test_mcp.json"

        MCPConfigManager(str(config_path))

        assert config_path.exists()
        with open(config_path) as f:
            config = json.load(f)
        assert config == {"servers": []}

    def test_init_uses_existing_config_file(self, tmp_path) -> None:
        """Test that init doesn't overwrite existing config file."""
        config_path = tmp_path / "test_mcp.json"
        existing_config = {
            "servers": [{"name": "test", "transport": "http", "url": "http://localhost"}]
        }
        config_path.write_text(json.dumps(existing_config))

        MCPConfigManager(str(config_path))

        with open(config_path) as f:
            config = json.load(f)
        assert config == existing_config

    def test_init_creates_parent_directory(self, tmp_path) -> None:
        """Test that init creates parent directory if it doesn't exist."""
        config_path = tmp_path / "subdir" / "test_mcp.json"

        MCPConfigManager(str(config_path))

        assert config_path.parent.exists()
        assert config_path.exists()

    def test_init_with_default_path(self) -> None:
        """Test that init uses default path when not specified."""
        with patch.object(
            Path, "expanduser", return_value=Path("/tmp/test/.gobby/mcp-servers.json")
        ):
            with patch.object(Path, "mkdir"):
                with patch.object(Path, "exists", return_value=True):
                    manager = MCPConfigManager()
                    assert "mcp-servers.json" in str(manager.config_path)


class TestMCPConfigManagerReadConfig:
    """Tests for _read_config method."""

    def test_read_config_valid_json(self, tmp_path) -> None:
        """Test reading valid JSON config."""
        config_path = tmp_path / "test_mcp.json"
        config_data = {
            "servers": [{"name": "test", "transport": "http", "url": "http://localhost"}]
        }
        config_path.write_text(json.dumps(config_data))

        manager = MCPConfigManager(str(config_path))
        config = manager._read_config()

        assert config == config_data

    def test_read_config_empty_file(self, tmp_path) -> None:
        """Test reading empty config file returns empty servers list."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text("")

        manager = MCPConfigManager(str(config_path))
        config = manager._read_config()

        assert config == {"servers": []}

    def test_read_config_missing_servers_key(self, tmp_path) -> None:
        """Test reading config without servers key adds it."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text(json.dumps({"other": "value"}))

        manager = MCPConfigManager(str(config_path))
        config = manager._read_config()

        assert "servers" in config
        assert config["servers"] == []

    def test_read_config_invalid_json(self, tmp_path) -> None:
        """Test reading invalid JSON raises ValueError."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text("not valid json {")

        manager = MCPConfigManager(str(config_path))

        with pytest.raises(ValueError, match="Invalid JSON"):
            manager._read_config()

    def test_read_config_not_object(self, tmp_path) -> None:
        """Test reading non-object JSON raises ValueError."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text(json.dumps(["array", "not", "object"]))

        manager = MCPConfigManager(str(config_path))

        with pytest.raises(ValueError, match="must be a JSON object"):
            manager._read_config()

    def test_read_config_servers_not_array(self, tmp_path) -> None:
        """Test reading config where servers is not array raises ValueError."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text(json.dumps({"servers": "not an array"}))

        manager = MCPConfigManager(str(config_path))

        with pytest.raises(ValueError, match="must be an array"):
            manager._read_config()


class TestMCPConfigManagerWriteConfig:
    """Tests for _write_config method."""

    def test_write_config_creates_file(self, tmp_path) -> None:
        """Test writing config creates file with correct content."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text(json.dumps({"servers": []}))  # Initial empty config

        manager = MCPConfigManager(str(config_path))
        manager._write_config({"servers": [{"name": "test"}]})

        with open(config_path) as f:
            config = json.load(f)
        assert config == {"servers": [{"name": "test"}]}

    def test_write_config_sets_permissions(self, tmp_path) -> None:
        """Test writing config sets restrictive permissions."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text(json.dumps({"servers": []}))

        manager = MCPConfigManager(str(config_path))
        manager._write_config({"servers": []})

        # Check permissions are 0o600 (owner read/write only)
        mode = config_path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_write_config_atomic_write(self, tmp_path) -> None:
        """Test that write uses atomic rename."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text(json.dumps({"servers": []}))

        manager = MCPConfigManager(str(config_path))

        # The temp file should not exist after successful write
        temp_path = config_path.with_suffix(".tmp")
        manager._write_config({"servers": [{"name": "test"}]})

        assert not temp_path.exists()
        assert config_path.exists()


class TestMCPConfigManagerLoadServers:
    """Tests for load_servers method."""

    def test_load_servers_empty(self, tmp_path) -> None:
        """Test loading empty server list."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text(json.dumps({"servers": []}))

        manager = MCPConfigManager(str(config_path))
        servers = manager.load_servers()

        assert servers == []

    def test_load_servers_http_transport(self, tmp_path) -> None:
        """Test loading server with HTTP transport."""
        config_path = tmp_path / "test_mcp.json"
        config_data = {
            "servers": [
                {
                    "name": "test-http",
                    "enabled": True,
                    "transport": "http",
                    "url": "http://localhost:8080/mcp",
                }
            ]
        }
        config_path.write_text(json.dumps(config_data))

        manager = MCPConfigManager(str(config_path))
        servers = manager.load_servers()

        assert len(servers) == 1
        assert servers[0].name == "test-http"
        assert servers[0].transport == "http"
        assert servers[0].url == "http://localhost:8080/mcp"

    def test_load_servers_stdio_transport(self, tmp_path) -> None:
        """Test loading server with stdio transport."""
        config_path = tmp_path / "test_mcp.json"
        config_data = {
            "servers": [
                {
                    "name": "test-stdio",
                    "enabled": True,
                    "transport": "stdio",
                    "command": "uvx",
                    "args": ["test-mcp"],
                }
            ]
        }
        config_path.write_text(json.dumps(config_data))

        manager = MCPConfigManager(str(config_path))
        servers = manager.load_servers()

        assert len(servers) == 1
        assert servers[0].name == "test-stdio"
        assert servers[0].transport == "stdio"
        assert servers[0].command == "uvx"
        assert servers[0].args == ["test-mcp"]

    def test_load_servers_skips_without_name(self, tmp_path) -> None:
        """Test that servers without name are skipped."""
        config_path = tmp_path / "test_mcp.json"
        config_data = {
            "servers": [
                {"transport": "http", "url": "http://localhost"},  # Missing name
                {"name": "valid", "transport": "http", "url": "http://localhost"},
            ]
        }
        config_path.write_text(json.dumps(config_data))

        manager = MCPConfigManager(str(config_path))
        servers = manager.load_servers()

        assert len(servers) == 1
        assert servers[0].name == "valid"

    def test_load_servers_with_oauth(self, tmp_path) -> None:
        """Test loading server with OAuth configuration."""
        config_path = tmp_path / "test_mcp.json"
        config_data = {
            "servers": [
                {
                    "name": "oauth-server",
                    "transport": "http",
                    "url": "http://localhost:8080/mcp",
                    "requires_oauth": True,
                    "oauth_provider": "github",
                }
            ]
        }
        config_path.write_text(json.dumps(config_data))

        manager = MCPConfigManager(str(config_path))
        servers = manager.load_servers()

        assert len(servers) == 1
        assert servers[0].requires_oauth is True
        assert servers[0].oauth_provider == "github"


class TestMCPConfigManagerSaveServers:
    """Tests for save_servers method."""

    def test_save_servers_empty_list(self, tmp_path) -> None:
        """Test saving empty server list."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text(json.dumps({"servers": []}))

        manager = MCPConfigManager(str(config_path))
        manager.save_servers([])

        with open(config_path) as f:
            config = json.load(f)
        assert config == {"servers": []}

    def test_save_servers_http_transport(self, tmp_path) -> None:
        """Test saving HTTP transport server."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text(json.dumps({"servers": []}))

        server = MCPServerConfig(
            name="test-http",
            project_id="global",
            enabled=True,
            transport="http",
            url="http://localhost:8080/mcp",
        )

        manager = MCPConfigManager(str(config_path))
        manager.save_servers([server])

        with open(config_path) as f:
            config = json.load(f)

        assert len(config["servers"]) == 1
        assert config["servers"][0]["name"] == "test-http"
        assert config["servers"][0]["transport"] == "http"
        assert config["servers"][0]["url"] == "http://localhost:8080/mcp"

    def test_save_servers_stdio_transport(self, tmp_path) -> None:
        """Test saving stdio transport server."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text(json.dumps({"servers": []}))

        server = MCPServerConfig(
            name="test-stdio",
            project_id="global",
            enabled=True,
            transport="stdio",
            command="uvx",
            args=["test-mcp"],
            env={"KEY": "value"},
        )

        manager = MCPConfigManager(str(config_path))
        manager.save_servers([server])

        with open(config_path) as f:
            config = json.load(f)

        assert config["servers"][0]["command"] == "uvx"
        assert config["servers"][0]["args"] == ["test-mcp"]
        assert config["servers"][0]["env"] == {"KEY": "value"}


class TestMCPConfigManagerAddServer:
    """Tests for add_server method."""

    def test_add_server_success(self, tmp_path) -> None:
        """Test adding a server successfully."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text(json.dumps({"servers": []}))

        server = MCPServerConfig(
            name="new-server",
            project_id="global",
            transport="http",
            url="http://localhost:8080/mcp",
        )

        manager = MCPConfigManager(str(config_path))
        manager.add_server(server)

        servers = manager.load_servers()
        assert len(servers) == 1
        assert servers[0].name == "new-server"

    def test_add_server_duplicate_name(self, tmp_path) -> None:
        """Test adding server with duplicate name raises ValueError."""
        config_path = tmp_path / "test_mcp.json"
        config_data = {
            "servers": [
                {"name": "existing", "transport": "http", "url": "http://localhost:8080/mcp"}
            ]
        }
        config_path.write_text(json.dumps(config_data))

        server = MCPServerConfig(
            name="existing", project_id="global", transport="http", url="http://localhost:9090/mcp"
        )

        manager = MCPConfigManager(str(config_path))

        with pytest.raises(ValueError, match="already exists"):
            manager.add_server(server)


class TestMCPConfigManagerRemoveServer:
    """Tests for remove_server method."""

    def test_remove_server_success(self, tmp_path) -> None:
        """Test removing a server successfully."""
        config_path = tmp_path / "test_mcp.json"
        config_data = {
            "servers": [
                {"name": "to-remove", "transport": "http", "url": "http://localhost:8080/mcp"}
            ]
        }
        config_path.write_text(json.dumps(config_data))

        manager = MCPConfigManager(str(config_path))
        manager.remove_server("to-remove")

        servers = manager.load_servers()
        assert len(servers) == 0

    def test_remove_server_not_found(self, tmp_path) -> None:
        """Test removing non-existent server raises ValueError."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text(json.dumps({"servers": []}))

        manager = MCPConfigManager(str(config_path))

        with pytest.raises(ValueError, match="not found"):
            manager.remove_server("non-existent")


class TestMCPConfigManagerUpdateServer:
    """Tests for update_server method."""

    def test_update_server_success(self, tmp_path) -> None:
        """Test updating a server successfully."""
        config_path = tmp_path / "test_mcp.json"
        config_data = {
            "servers": [
                {"name": "to-update", "transport": "http", "url": "http://localhost:8080/mcp"}
            ]
        }
        config_path.write_text(json.dumps(config_data))

        updated_server = MCPServerConfig(
            name="to-update",
            project_id="global",
            transport="http",
            url="http://localhost:9090/mcp",  # Changed URL
        )

        manager = MCPConfigManager(str(config_path))
        manager.update_server(updated_server)

        servers = manager.load_servers()
        assert len(servers) == 1
        assert servers[0].url == "http://localhost:9090/mcp"

    def test_update_server_not_found(self, tmp_path) -> None:
        """Test updating non-existent server raises ValueError."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text(json.dumps({"servers": []}))

        server = MCPServerConfig(
            name="non-existent",
            project_id="global",
            transport="http",
            url="http://localhost:8080/mcp",
        )

        manager = MCPConfigManager(str(config_path))

        with pytest.raises(ValueError, match="not found"):
            manager.update_server(server)


class TestMCPConfigManagerGetServer:
    """Tests for get_server method."""

    def test_get_server_found(self, tmp_path) -> None:
        """Test getting an existing server."""
        config_path = tmp_path / "test_mcp.json"
        config_data = {
            "servers": [
                {"name": "test-server", "transport": "http", "url": "http://localhost:8080/mcp"}
            ]
        }
        config_path.write_text(json.dumps(config_data))

        manager = MCPConfigManager(str(config_path))
        server = manager.get_server("test-server")

        assert server is not None
        assert server.name == "test-server"

    def test_get_server_not_found(self, tmp_path) -> None:
        """Test getting non-existent server returns None."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text(json.dumps({"servers": []}))

        manager = MCPConfigManager(str(config_path))
        server = manager.get_server("non-existent")

        assert server is None


class TestMCPConfigManagerListServers:
    """Tests for list_servers method."""

    def test_list_servers_empty(self, tmp_path) -> None:
        """Test listing servers when empty."""
        config_path = tmp_path / "test_mcp.json"
        config_path.write_text(json.dumps({"servers": []}))

        manager = MCPConfigManager(str(config_path))
        names = manager.list_servers()

        assert names == []

    def test_list_servers_multiple(self, tmp_path) -> None:
        """Test listing multiple servers."""
        config_path = tmp_path / "test_mcp.json"
        config_data = {
            "servers": [
                {"name": "server1", "transport": "http", "url": "http://localhost:8080"},
                {"name": "server2", "transport": "http", "url": "http://localhost:8081"},
                {"name": "server3", "transport": "http", "url": "http://localhost:8082"},
            ]
        }
        config_path.write_text(json.dumps(config_data))

        manager = MCPConfigManager(str(config_path))
        names = manager.list_servers()

        assert names == ["server1", "server2", "server3"]


class TestMigrateLegacyMcpConfig:
    """Tests for migrate_legacy_mcp_config helper."""

    def test_renames_legacy_when_new_missing(self, tmp_path) -> None:
        legacy = tmp_path / ".mcp.json"
        new = tmp_path / "mcp-servers.json"
        legacy.write_text('{"servers": []}')

        assert migrate_legacy_mcp_config(new_path=new, legacy_path=legacy) is True

        assert not legacy.exists()
        assert new.read_text() == '{"servers": []}'

    def test_no_op_when_legacy_missing(self, tmp_path) -> None:
        legacy = tmp_path / ".mcp.json"
        new = tmp_path / "mcp-servers.json"

        assert migrate_legacy_mcp_config(new_path=new, legacy_path=legacy) is False
        assert not new.exists()

    def test_no_op_when_both_exist(self, tmp_path) -> None:
        legacy = tmp_path / ".mcp.json"
        new = tmp_path / "mcp-servers.json"
        legacy.write_text("legacy")
        new.write_text("new")

        assert migrate_legacy_mcp_config(new_path=new, legacy_path=legacy) is False

        assert legacy.read_text() == "legacy"
        assert new.read_text() == "new"

    def test_creates_parent_directory(self, tmp_path) -> None:
        legacy = tmp_path / ".mcp.json"
        new = tmp_path / "nested" / "mcp-servers.json"
        legacy.write_text('{"servers": []}')

        assert migrate_legacy_mcp_config(new_path=new, legacy_path=legacy) is True

        assert new.parent.exists()
        assert new.read_text() == '{"servers": []}'
