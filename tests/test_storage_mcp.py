"""Tests for the LocalMCPManager storage layer."""

import json
from pathlib import Path

from gobby.storage.mcp import LocalMCPManager


class TestMCPServer:
    """Tests for MCPServer dataclass."""

    def test_to_dict(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test converting MCPServer to dictionary."""
        server = mcp_manager.upsert(
            name="test-server",
            transport="http",
            url="http://localhost:8080",
            project_id=sample_project["id"],
            description="Test server",
        )

        d = server.to_dict()
        assert d["name"] == "test-server"
        assert d["transport"] == "http"
        assert d["url"] == "http://localhost:8080"
        assert d["enabled"] is True
        assert d["description"] == "Test server"

    def test_to_config(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test converting MCPServer to MCP config format."""
        server = mcp_manager.upsert(
            name="config-server",
            transport="stdio",
            command="npx",
            args=["-y", "@test/server"],
            env={"API_KEY": "secret"},
            project_id=sample_project["id"],
        )

        config = server.to_config()
        assert config["name"] == "config-server"
        assert config["transport"] == "stdio"
        assert config["command"] == "npx"
        assert config["args"] == ["-y", "@test/server"]
        assert config["env"] == {"API_KEY": "secret"}


class TestTool:
    """Tests for Tool dataclass."""

    def test_to_dict(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test converting Tool to dictionary."""
        # Create server first
        mcp_manager.upsert(
            name="tool-server",
            transport="http",
            url="http://localhost:8080",
            project_id=sample_project["id"],
        )

        # Cache a tool
        mcp_manager.cache_tools(
            "tool-server",
            [
                {
                    "name": "my_tool",
                    "description": "Does something",
                    "inputSchema": {"type": "object"},
                }
            ],
            project_id=sample_project["id"],
        )

        tools = mcp_manager.get_cached_tools("tool-server", project_id=sample_project["id"])
        assert len(tools) == 1

        d = tools[0].to_dict()
        assert d["name"] == "my_tool"
        assert d["description"] == "Does something"


class TestLocalMCPManager:
    """Tests for LocalMCPManager class."""

    def test_upsert_http_server(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test upserting an HTTP MCP server."""
        server = mcp_manager.upsert(
            name="http-server",
            transport="http",
            url="http://localhost:8080/mcp",
            headers={"Authorization": "Bearer token"},
            project_id=sample_project["id"],
        )

        assert server.id is not None
        assert server.name == "http-server"
        assert server.transport == "http"
        assert server.url == "http://localhost:8080/mcp"
        assert server.headers == {"Authorization": "Bearer token"}

    def test_upsert_stdio_server(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test upserting a stdio MCP server."""
        server = mcp_manager.upsert(
            name="stdio-server",
            transport="stdio",
            command="npx",
            args=["-y", "@anthropic/mcp-server"],
            env={"DEBUG": "true"},
            project_id=sample_project["id"],
        )

        assert server.name == "stdio-server"
        assert server.transport == "stdio"
        assert server.command == "npx"
        assert server.args == ["-y", "@anthropic/mcp-server"]
        assert server.env == {"DEBUG": "true"}

    def test_upsert_normalizes_name_to_lowercase(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test that server names are normalized to lowercase."""
        server = mcp_manager.upsert(
            name="MyServer",
            transport="http",
            url="http://localhost:8080",
            project_id=sample_project["id"],
        )

        assert server.name == "myserver"

    def test_upsert_updates_existing(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test that upsert updates existing server."""
        server1 = mcp_manager.upsert(
            name="update-server",
            transport="http",
            url="http://old-url",
            project_id=sample_project["id"],
        )

        server2 = mcp_manager.upsert(
            name="update-server",
            transport="http",
            url="http://new-url",
            project_id=sample_project["id"],
        )

        # Should be same server with updated URL
        assert server2.id == server1.id
        assert server2.url == "http://new-url"

    def test_get_server(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test getting a server by name."""
        created = mcp_manager.upsert(
            name="get-test",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        retrieved = mcp_manager.get_server("get-test", project_id=sample_project["id"])
        assert retrieved is not None
        assert retrieved.id == created.id

    def test_get_server_case_insensitive(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test that get_server lookup is case-insensitive."""
        mcp_manager.upsert(
            name="casetest",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        # Should find regardless of case
        assert mcp_manager.get_server("CASETEST", project_id=sample_project["id"]) is not None
        assert mcp_manager.get_server("CaseTest", project_id=sample_project["id"]) is not None
        assert mcp_manager.get_server("casetest", project_id=sample_project["id"]) is not None

    def test_get_server_nonexistent(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test getting nonexistent server returns None."""
        result = mcp_manager.get_server("nonexistent", project_id=sample_project["id"])
        assert result is None

    def test_list_servers(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test listing servers."""
        mcp_manager.upsert(
            name="server-1",
            transport="http",
            url="http://localhost:8001",
            project_id=sample_project["id"],
        )
        mcp_manager.upsert(
            name="server-2",
            transport="http",
            url="http://localhost:8002",
            project_id=sample_project["id"],
        )

        servers = mcp_manager.list_servers(project_id=sample_project["id"])
        assert len(servers) == 2
        names = [s.name for s in servers]
        assert "server-1" in names
        assert "server-2" in names

    def test_list_servers_enabled_only(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test listing only enabled servers."""
        mcp_manager.upsert(
            name="enabled-server",
            transport="http",
            url="http://localhost",
            enabled=True,
            project_id=sample_project["id"],
        )
        mcp_manager.upsert(
            name="disabled-server",
            transport="http",
            url="http://localhost",
            enabled=False,
            project_id=sample_project["id"],
        )

        enabled = mcp_manager.list_servers(project_id=sample_project["id"], enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0].name == "enabled-server"

        all_servers = mcp_manager.list_servers(project_id=sample_project["id"], enabled_only=False)
        assert len(all_servers) == 2

    def test_update_server(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test updating server fields."""
        mcp_manager.upsert(
            name="update-me",
            transport="http",
            url="http://old-url",
            project_id=sample_project["id"],
        )

        updated = mcp_manager.update_server(
            "update-me",
            project_id=sample_project["id"],
            url="http://new-url",
            enabled=False,
        )

        assert updated is not None
        assert updated.url == "http://new-url"
        assert updated.enabled is False

    def test_remove_server(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test removing a server."""
        mcp_manager.upsert(
            name="remove-me",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        result = mcp_manager.remove_server("remove-me", project_id=sample_project["id"])
        assert result is True
        assert mcp_manager.get_server("remove-me", project_id=sample_project["id"]) is None

    def test_remove_nonexistent(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test removing nonexistent server returns False."""
        result = mcp_manager.remove_server("nonexistent", project_id=sample_project["id"])
        assert result is False

    def test_cache_tools(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test caching tools for a server."""
        mcp_manager.upsert(
            name="tools-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        tools = [
            {
                "name": "tool_one",
                "description": "First tool",
                "inputSchema": {"type": "object", "properties": {"arg1": {"type": "string"}}},
            },
            {
                "name": "tool_two",
                "description": "Second tool",
                "inputSchema": {"type": "object"},
            },
        ]

        count = mcp_manager.cache_tools("tools-server", tools, project_id=sample_project["id"])
        assert count == 2

    def test_cache_tools_normalizes_name(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test that tool names are normalized to lowercase."""
        mcp_manager.upsert(
            name="normalize-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        mcp_manager.cache_tools(
            "normalize-server",
            [{"name": "MyTool", "description": "Test"}],
            project_id=sample_project["id"],
        )

        tools = mcp_manager.get_cached_tools("normalize-server", project_id=sample_project["id"])
        assert tools[0].name == "mytool"

    def test_cache_tools_replaces_existing(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test that caching tools replaces existing tools."""
        mcp_manager.upsert(
            name="replace-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        # First cache
        mcp_manager.cache_tools(
            "replace-server",
            [{"name": "old_tool", "description": "Old"}],
            project_id=sample_project["id"],
        )

        # Second cache replaces
        mcp_manager.cache_tools(
            "replace-server",
            [{"name": "new_tool", "description": "New"}],
            project_id=sample_project["id"],
        )

        tools = mcp_manager.get_cached_tools("replace-server", project_id=sample_project["id"])
        assert len(tools) == 1
        assert tools[0].name == "new_tool"

    def test_get_cached_tools(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test getting cached tools for a server."""
        mcp_manager.upsert(
            name="cached-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        mcp_manager.cache_tools(
            "cached-server",
            [
                {"name": "alpha", "description": "A tool"},
                {"name": "beta", "description": "B tool"},
            ],
            project_id=sample_project["id"],
        )

        tools = mcp_manager.get_cached_tools("cached-server", project_id=sample_project["id"])
        assert len(tools) == 2
        names = [t.name for t in tools]
        assert "alpha" in names
        assert "beta" in names

    def test_get_cached_tools_nonexistent_server(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test getting tools for nonexistent server returns empty list."""
        tools = mcp_manager.get_cached_tools("nonexistent", project_id=sample_project["id"])
        assert tools == []

    def test_import_from_mcp_json_gobby_format(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_dir: Path,
    ):
        """Test importing servers from Gobby-format .mcp.json."""
        mcp_json = temp_dir / ".mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "servers": [
                        {
                            "name": "gobby-server",
                            "transport": "http",
                            "url": "http://localhost:8080",
                        }
                    ]
                }
            )
        )

        count = mcp_manager.import_from_mcp_json(mcp_json, project_id=sample_project["id"])
        assert count == 1

        server = mcp_manager.get_server("gobby-server", project_id=sample_project["id"])
        assert server is not None
        assert server.url == "http://localhost:8080"

    def test_import_from_mcp_json_claude_format(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_dir: Path,
    ):
        """Test importing servers from Claude Code format .mcp.json."""
        mcp_json = temp_dir / ".mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "claude-server": {
                            "transport": "stdio",
                            "command": "npx",
                            "args": ["-y", "@test/server"],
                        }
                    }
                }
            )
        )

        count = mcp_manager.import_from_mcp_json(mcp_json, project_id=sample_project["id"])
        assert count == 1

        server = mcp_manager.get_server("claude-server", project_id=sample_project["id"])
        assert server is not None
        assert server.command == "npx"

    def test_import_from_nonexistent_file(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test importing from nonexistent file returns 0."""
        count = mcp_manager.import_from_mcp_json(
            "/nonexistent/path.json",
            project_id=sample_project["id"],
        )
        assert count == 0

    def test_import_tools_from_filesystem(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_dir: Path,
    ):
        """Test importing tool schemas from filesystem."""
        # Create server first
        mcp_manager.upsert(
            name="fs-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        # Create tool schema files
        tools_dir = temp_dir / "tools" / "fs-server"
        tools_dir.mkdir(parents=True)

        (tools_dir / "my_tool.json").write_text(
            json.dumps(
                {
                    "name": "my_tool",
                    "description": "A filesystem tool",
                    "inputSchema": {"type": "object"},
                }
            )
        )

        count = mcp_manager.import_tools_from_filesystem(
            project_id=sample_project["id"],
            tools_dir=temp_dir / "tools",
        )

        assert count == 1
        tools = mcp_manager.get_cached_tools("fs-server", project_id=sample_project["id"])
        assert len(tools) == 1
        assert tools[0].description == "A filesystem tool"
