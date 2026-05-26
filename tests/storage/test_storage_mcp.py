"""Tests for the LocalMCPManager storage layer."""

import json
from pathlib import Path

import pytest

from gobby.mcp_proxy.bundled import CHROME_DEVTOOLS_NPM_PACKAGE
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.projects import GLOBAL_PROJECT_ID, LocalProjectManager

pytestmark = pytest.mark.unit


class TestMCPServer:
    """Tests for MCPServer dataclass."""

    def test_to_dict(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
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
    ) -> None:
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
    ) -> None:
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
    ) -> None:
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
    ) -> None:
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
    ) -> None:
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
    ) -> None:
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

    def test_upsert_bundled_server_uses_global_project_and_strips_runtime_args(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Bundled servers are stored globally and never persist runtime-only browser paths."""
        server = mcp_manager.upsert(
            name="chrome-devtools",
            transport="stdio",
            command="npx",
            args=[
                "-y",
                CHROME_DEVTOOLS_NPM_PACKAGE,
                "--executable-path=/tmp/chrome",
                "--no-usage-statistics",
            ],
            project_id=sample_project["id"],
        )

        assert server.project_id == GLOBAL_PROJECT_ID
        assert server.args == ["-y", CHROME_DEVTOOLS_NPM_PACKAGE, "--no-usage-statistics"]

        project_row = temp_db.fetchone(
            "SELECT * FROM mcp_servers WHERE name = ? AND project_id = ?",
            ("chrome-devtools", sample_project["id"]),
        )
        assert project_row is None

    def test_get_server(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
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
    ) -> None:
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
    ) -> None:
        """Test getting nonexistent server returns None."""
        result = mcp_manager.get_server("nonexistent", project_id=sample_project["id"])
        assert result is None

    def test_list_servers(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
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
    ) -> None:
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
    ) -> None:
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
    ) -> None:
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
    ) -> None:
        """Test removing nonexistent server returns False."""
        result = mcp_manager.remove_server("nonexistent", project_id=sample_project["id"])
        assert result is False

    def test_cache_tools(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
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
    ) -> None:
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
    ) -> None:
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
    ) -> None:
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
    ) -> None:
        """Test getting tools for nonexistent server returns empty list."""
        tools = mcp_manager.get_cached_tools("nonexistent", project_id=sample_project["id"])
        assert tools == []

    def test_import_from_mcp_json_gobby_format(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_dir: Path,
    ) -> None:
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
    ) -> None:
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
    ) -> None:
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
    ) -> None:
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

    def test_import_tools_from_filesystem_nonexistent_dir(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test importing from nonexistent directory returns 0."""
        count = mcp_manager.import_tools_from_filesystem(
            project_id=sample_project["id"],
            tools_dir="/nonexistent/path",
        )
        assert count == 0

    def test_import_tools_from_filesystem_skips_hidden_dirs(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_dir: Path,
    ) -> None:
        """Test that hidden directories are skipped during import."""
        # Create server
        mcp_manager.upsert(
            name=".hidden-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        # Create hidden tool directory
        tools_dir = temp_dir / "tools" / ".hidden-server"
        tools_dir.mkdir(parents=True)
        (tools_dir / "tool.json").write_text(json.dumps({"name": "tool", "description": "Hidden"}))

        count = mcp_manager.import_tools_from_filesystem(
            project_id=sample_project["id"],
            tools_dir=temp_dir / "tools",
        )
        assert count == 0

    def test_import_tools_from_filesystem_skips_unknown_server(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_dir: Path,
    ) -> None:
        """Test that tools for unknown servers are skipped."""
        # Create tool directory without corresponding server
        tools_dir = temp_dir / "tools" / "unknown-server"
        tools_dir.mkdir(parents=True)
        (tools_dir / "tool.json").write_text(json.dumps({"name": "tool", "description": "Unknown"}))

        count = mcp_manager.import_tools_from_filesystem(
            project_id=sample_project["id"],
            tools_dir=temp_dir / "tools",
        )
        assert count == 0

    def test_import_tools_from_filesystem_handles_invalid_json(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_dir: Path,
    ) -> None:
        """Test that invalid JSON files are gracefully skipped."""
        mcp_manager.upsert(
            name="json-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        tools_dir = temp_dir / "tools" / "json-server"
        tools_dir.mkdir(parents=True)
        (tools_dir / "valid.json").write_text(
            json.dumps({"name": "valid_tool", "description": "Valid"})
        )
        (tools_dir / "invalid.json").write_text("{ not valid json }")

        count = mcp_manager.import_tools_from_filesystem(
            project_id=sample_project["id"],
            tools_dir=temp_dir / "tools",
        )
        # Only the valid tool should be imported
        assert count == 1
        tools = mcp_manager.get_cached_tools("json-server", project_id=sample_project["id"])
        assert len(tools) == 1
        assert tools[0].name == "valid_tool"

    def test_import_tools_from_filesystem_uses_stem_for_name(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_dir: Path,
    ) -> None:
        """Test that tool name defaults to file stem if not in JSON."""
        mcp_manager.upsert(
            name="stem-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        tools_dir = temp_dir / "tools" / "stem-server"
        tools_dir.mkdir(parents=True)
        # JSON without name field
        (tools_dir / "my_tool_name.json").write_text(
            json.dumps({"description": "Tool without name"})
        )

        count = mcp_manager.import_tools_from_filesystem(
            project_id=sample_project["id"],
            tools_dir=temp_dir / "tools",
        )
        assert count == 1
        tools = mcp_manager.get_cached_tools("stem-server", project_id=sample_project["id"])
        assert len(tools) == 1
        assert tools[0].name == "my_tool_name"

    def test_get_server_by_id(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test getting a server by ID."""
        created = mcp_manager.upsert(
            name="id-test",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        retrieved = mcp_manager.get_server_by_id(created.id)
        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.name == "id-test"

    def test_get_server_by_id_nonexistent(
        self,
        mcp_manager: LocalMCPManager,
    ) -> None:
        """Test getting nonexistent server by ID returns None."""
        result = mcp_manager.get_server_by_id("nonexistent-uuid")
        assert result is None

    def test_list_all_servers(
        self,
        mcp_manager: LocalMCPManager,
        project_manager: LocalProjectManager,
        sample_project: dict,
    ) -> None:
        """Test listing all servers across all projects."""
        # Create another project
        project2 = project_manager.create(
            name="project-2",
            repo_path="/tmp/project-2",
        )

        # Add servers to both projects
        mcp_manager.upsert(
            name="server-p1",
            transport="http",
            url="http://localhost:8001",
            project_id=sample_project["id"],
        )
        mcp_manager.upsert(
            name="server-p2",
            transport="http",
            url="http://localhost:8002",
            project_id=project2.id,
        )

        all_servers = mcp_manager.list_all_servers(enabled_only=False)
        names = [s.name for s in all_servers]
        assert "server-p1" in names
        assert "server-p2" in names

    def test_list_all_servers_enabled_only(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test list_all_servers with enabled_only filter."""
        mcp_manager.upsert(
            name="enabled-all",
            transport="http",
            url="http://localhost",
            enabled=True,
            project_id=sample_project["id"],
        )
        mcp_manager.upsert(
            name="disabled-all",
            transport="http",
            url="http://localhost",
            enabled=False,
            project_id=sample_project["id"],
        )

        enabled = mcp_manager.list_all_servers(enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0].name == "enabled-all"

        all_servers = mcp_manager.list_all_servers(enabled_only=False)
        assert len(all_servers) == 2

    def test_list_runtime_servers_includes_global_bundled_servers(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Runtime server listing includes bundled global servers for project contexts."""
        mcp_manager.upsert(
            name="context7",
            transport="stdio",
            command="npx",
            args=["-y", "@upstash/context7-mcp"],
            project_id=GLOBAL_PROJECT_ID,
        )
        mcp_manager.upsert(
            name="global-not-bundled",
            transport="http",
            url="http://localhost:7000",
            project_id=GLOBAL_PROJECT_ID,
        )
        mcp_manager.upsert(
            name="project-only",
            transport="http",
            url="http://localhost:9000",
            project_id=sample_project["id"],
        )

        project_servers = mcp_manager.list_servers(project_id=sample_project["id"])
        runtime_servers = mcp_manager.list_runtime_servers(project_id=sample_project["id"])

        assert [server.name for server in project_servers] == ["project-only"]
        runtime_names = {server.name for server in runtime_servers}
        assert runtime_names == {"context7", "project-only"}

    def test_update_server_nonexistent(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test updating nonexistent server returns None."""
        result = mcp_manager.update_server(
            "nonexistent",
            project_id=sample_project["id"],
            url="http://new-url",
        )
        assert result is None

    def test_update_server_no_valid_fields(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test updating with no valid fields returns unchanged server."""
        original = mcp_manager.upsert(
            name="no-update",
            transport="http",
            url="http://original",
            project_id=sample_project["id"],
        )

        updated = mcp_manager.update_server(
            "no-update",
            project_id=sample_project["id"],
            invalid_field="ignored",
        )

        assert updated is not None
        assert updated.url == original.url

    def test_update_server_json_fields(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test updating JSON-serializable fields (args, env, headers)."""
        mcp_manager.upsert(
            name="json-update",
            transport="stdio",
            command="node",
            project_id=sample_project["id"],
        )

        updated = mcp_manager.update_server(
            "json-update",
            project_id=sample_project["id"],
            args=["--verbose", "--debug"],
            env={"NODE_ENV": "test"},
            headers={"X-Custom": "header"},
        )

        assert updated is not None
        assert updated.args == ["--verbose", "--debug"]
        assert updated.env == {"NODE_ENV": "test"}
        assert updated.headers == {"X-Custom": "header"}

    def test_cache_tools_nonexistent_server(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test caching tools for nonexistent server returns 0."""
        count = mcp_manager.cache_tools(
            "nonexistent-server",
            [{"name": "tool", "description": "Test"}],
            project_id=sample_project["id"],
        )
        assert count == 0

    def test_cache_tools_with_args_key(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test caching tools using 'args' key instead of 'inputSchema'."""
        mcp_manager.upsert(
            name="args-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        mcp_manager.cache_tools(
            "args-server",
            [
                {
                    "name": "args_tool",
                    "description": "Tool with args",
                    "args": {"type": "object", "properties": {"foo": {"type": "string"}}},
                }
            ],
            project_id=sample_project["id"],
        )

        tools = mcp_manager.get_cached_tools("args-server", project_id=sample_project["id"])
        assert len(tools) == 1
        assert tools[0].input_schema == {
            "type": "object",
            "properties": {"foo": {"type": "string"}},
        }

    def test_cache_tools_without_schema(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test caching tools without inputSchema or args."""
        mcp_manager.upsert(
            name="no-schema-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        mcp_manager.cache_tools(
            "no-schema-server",
            [{"name": "simple_tool", "description": "No schema"}],
            project_id=sample_project["id"],
        )

        tools = mcp_manager.get_cached_tools("no-schema-server", project_id=sample_project["id"])
        assert len(tools) == 1
        assert tools[0].input_schema is None

    def test_import_from_mcp_json_invalid_json(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_dir: Path,
    ) -> None:
        """Test importing from invalid JSON file returns 0."""
        mcp_json = temp_dir / ".mcp.json"
        mcp_json.write_text("{ invalid json }")

        count = mcp_manager.import_from_mcp_json(mcp_json, project_id=sample_project["id"])
        assert count == 0

    def test_import_from_mcp_json_gobby_format_skip_nameless(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_dir: Path,
    ) -> None:
        """Test that servers without name are skipped in Gobby format."""
        mcp_json = temp_dir / ".mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "servers": [
                        {"transport": "http", "url": "http://no-name"},  # No name
                        {"name": "named-server", "transport": "http", "url": "http://named"},
                    ]
                }
            )
        )

        count = mcp_manager.import_from_mcp_json(mcp_json, project_id=sample_project["id"])
        assert count == 1

        server = mcp_manager.get_server("named-server", project_id=sample_project["id"])
        assert server is not None

    def test_import_from_mcp_json_empty_format(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_dir: Path,
    ) -> None:
        """Test importing from JSON without servers or mcpServers returns 0."""
        mcp_json = temp_dir / ".mcp.json"
        mcp_json.write_text(json.dumps({"other_key": "value"}))

        count = mcp_manager.import_from_mcp_json(mcp_json, project_id=sample_project["id"])
        assert count == 0

    def test_remove_server_case_insensitive(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test that remove_server is case-insensitive."""
        mcp_manager.upsert(
            name="removecase",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        # Remove with different case
        result = mcp_manager.remove_server("REMOVECASE", project_id=sample_project["id"])
        assert result is True
        assert mcp_manager.get_server("removecase", project_id=sample_project["id"]) is None

    def test_normalize_bundled_servers_migrates_tools_from_legacy_project_rows(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Legacy project rows are collapsed into a canonical global bundled row."""
        temp_db.execute(
            """
            INSERT INTO mcp_servers (
                id, name, project_id, transport, command, args, enabled, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                "legacy-chrome-server",
                "chrome-devtools",
                sample_project["id"],
                "stdio",
                "npx",
                json.dumps(
                    [
                        "-y",
                        CHROME_DEVTOOLS_NPM_PACKAGE,
                        "--executable-path=/tmp/chrome",
                        "--no-usage-statistics",
                    ]
                ),
                True,
            ),
        )
        temp_db.execute(
            """
            INSERT INTO tools (id, mcp_server_id, name, description, input_schema, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                "legacy-chrome-tool",
                "legacy-chrome-server",
                "inspect_page",
                "Inspect the current page",
                json.dumps({"type": "object"}),
            ),
        )

        stats = mcp_manager.normalize_bundled_servers(["chrome-devtools"])

        assert stats["normalized"] == 1
        assert stats["duplicates_removed"] == 1
        assert stats["tools_migrated"] == 1

        global_server = mcp_manager.get_server("chrome-devtools", project_id=GLOBAL_PROJECT_ID)
        assert global_server is not None
        assert global_server.project_id == GLOBAL_PROJECT_ID
        assert global_server.args == ["-y", CHROME_DEVTOOLS_NPM_PACKAGE, "--no-usage-statistics"]

        legacy_row = temp_db.fetchone(
            "SELECT * FROM mcp_servers WHERE id = ?",
            ("legacy-chrome-server",),
        )
        assert legacy_row is None

        migrated_tools = mcp_manager.get_cached_tools(
            "chrome-devtools",
            project_id=GLOBAL_PROJECT_ID,
        )
        assert len(migrated_tools) == 1
        assert migrated_tools[0].name == "inspect_page"

    def test_normalize_bundled_servers_updates_chrome_devtools_package_pin(
        self,
        mcp_manager: LocalMCPManager,
        temp_db: HubDatabase,
    ) -> None:
        """Existing bundled chrome-devtools rows are repaired to the tested package pin."""
        temp_db.execute(
            """
            INSERT INTO mcp_servers (
                id, name, project_id, transport, command, args, enabled, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                "global-chrome-server",
                "chrome-devtools",
                GLOBAL_PROJECT_ID,
                "stdio",
                "npx",
                json.dumps(["-y", "chrome-devtools-mcp@latest", "--no-usage-statistics"]),
                True,
            ),
        )

        stats = mcp_manager.normalize_bundled_servers(["chrome-devtools"])

        assert stats["normalized"] == 1
        global_server = mcp_manager.get_server("chrome-devtools", project_id=GLOBAL_PROJECT_ID)
        assert global_server is not None
        assert global_server.args == ["-y", CHROME_DEVTOOLS_NPM_PACKAGE, "--no-usage-statistics"]

    def test_normalize_bundled_servers_unions_disjoint_tool_sets(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Bundled normalization preserves the union of tool names across duplicates."""
        temp_db.execute(
            """
            INSERT INTO mcp_servers (
                id, name, project_id, transport, command, args, enabled, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                "legacy-context7-server",
                "context7",
                sample_project["id"],
                "stdio",
                "npx",
                json.dumps(["-y", "@upstash/context7-mcp"]),
                True,
            ),
        )
        temp_db.execute(
            """
            INSERT INTO tools (id, mcp_server_id, name, description, input_schema, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                "legacy-context7-search",
                "legacy-context7-server",
                "search_docs",
                "Search docs",
                json.dumps({"type": "object"}),
            ),
        )

        temp_db.execute(
            """
            INSERT INTO mcp_servers (
                id, name, project_id, transport, command, args, enabled, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                "global-context7-server",
                "context7",
                GLOBAL_PROJECT_ID,
                "stdio",
                "npx",
                json.dumps(["-y", "@upstash/context7-mcp"]),
                True,
            ),
        )
        temp_db.execute(
            """
            INSERT INTO tools (id, mcp_server_id, name, description, input_schema, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                "global-context7-resolve",
                "global-context7-server",
                "resolve_doc",
                "Resolve doc",
                json.dumps({"type": "object"}),
            ),
        )

        mcp_manager.normalize_bundled_servers(["context7"])

        tools = mcp_manager.get_cached_tools("context7", project_id=GLOBAL_PROJECT_ID)
        global_server = mcp_manager.get_server("context7", project_id=GLOBAL_PROJECT_ID)
        assert global_server is not None
        assert {tool.name for tool in tools} == {"resolve_doc", "search_docs"}

        legacy_row = temp_db.fetchone(
            "SELECT * FROM mcp_servers WHERE id = ?",
            ("legacy-context7-server",),
        )
        assert legacy_row is None


class TestRefreshToolsIncremental:
    """Tests for the refresh_tools_incremental method."""

    def test_refresh_tools_incremental_nonexistent_server(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test incremental refresh for nonexistent server returns empty stats."""
        stats = mcp_manager.refresh_tools_incremental(
            "nonexistent",
            [{"name": "tool", "inputSchema": {}}],
            project_id=sample_project["id"],
        )
        assert stats == {"added": 0, "updated": 0, "removed": 0, "unchanged": 0, "total": 0}

    def test_refresh_tools_incremental_adds_new_tools(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test that new tools are added during incremental refresh."""
        mcp_manager.upsert(
            name="refresh-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        stats = mcp_manager.refresh_tools_incremental(
            "refresh-server",
            [
                {"name": "new_tool_1", "description": "First", "inputSchema": {"type": "object"}},
                {"name": "new_tool_2", "description": "Second", "inputSchema": {"type": "object"}},
            ],
            project_id=sample_project["id"],
        )

        assert stats["added"] == 2
        assert stats["total"] == 2

        tools = mcp_manager.get_cached_tools("refresh-server", project_id=sample_project["id"])
        assert len(tools) == 2

    def test_refresh_tools_incremental_removes_stale_tools(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test that stale tools are removed during incremental refresh."""
        mcp_manager.upsert(
            name="stale-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        # Cache initial tools
        mcp_manager.cache_tools(
            "stale-server",
            [
                {"name": "keep_tool", "description": "Keep"},
                {"name": "stale_tool", "description": "Remove"},
            ],
            project_id=sample_project["id"],
        )

        # Refresh with only one tool (no schema_hash_manager, so all treated as changed)
        stats = mcp_manager.refresh_tools_incremental(
            "stale-server",
            [{"name": "keep_tool", "description": "Keep Updated"}],
            project_id=sample_project["id"],
        )

        assert stats["removed"] == 1
        assert stats["total"] == 1

        tools = mcp_manager.get_cached_tools("stale-server", project_id=sample_project["id"])
        assert len(tools) == 1
        assert tools[0].name == "keep_tool"

    def test_refresh_tools_incremental_updates_changed_tools(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test that changed tools are updated during incremental refresh."""
        mcp_manager.upsert(
            name="update-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        # Cache initial tool
        mcp_manager.cache_tools(
            "update-server",
            [{"name": "change_tool", "description": "Original", "inputSchema": {"type": "object"}}],
            project_id=sample_project["id"],
        )

        # Refresh with updated tool (no schema_hash_manager)
        stats = mcp_manager.refresh_tools_incremental(
            "update-server",
            [
                {
                    "name": "change_tool",
                    "description": "Updated",
                    "inputSchema": {"type": "object", "updated": True},
                }
            ],
            project_id=sample_project["id"],
        )

        # Without schema_hash_manager, exactly one tool change should be recorded
        assert stats["updated"] + stats["added"] == 1
        assert stats.get("removed", 0) == 0

        tools = mcp_manager.get_cached_tools("update-server", project_id=sample_project["id"])
        assert len(tools) == 1
        assert tools[0].description == "Updated"

    def test_refresh_tools_incremental_with_schema_hash_manager(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_db: HubDatabase,
    ) -> None:
        """Test incremental refresh with schema hash manager for change detection."""
        from gobby.mcp_proxy.schema_hash import SchemaHashManager

        schema_hash_manager = SchemaHashManager(temp_db)

        mcp_manager.upsert(
            name="hash-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        # Initial refresh to establish hashes
        initial_tools = [
            {"name": "unchanged_tool", "inputSchema": {"type": "object"}},
            {"name": "will_change_tool", "inputSchema": {"type": "string"}},
        ]
        stats1 = mcp_manager.refresh_tools_incremental(
            "hash-server",
            initial_tools,
            project_id=sample_project["id"],
            schema_hash_manager=schema_hash_manager,
        )
        assert stats1["added"] == 2

        # Second refresh with one changed, one unchanged, one new
        updated_tools = [
            {"name": "unchanged_tool", "inputSchema": {"type": "object"}},  # Same
            {"name": "will_change_tool", "inputSchema": {"type": "number"}},  # Changed
            {"name": "new_tool", "inputSchema": {"type": "boolean"}},  # New
        ]
        stats2 = mcp_manager.refresh_tools_incremental(
            "hash-server",
            updated_tools,
            project_id=sample_project["id"],
            schema_hash_manager=schema_hash_manager,
        )

        assert stats2["unchanged"] == 1
        assert stats2["updated"] == 1
        assert stats2["added"] == 1

    def test_refresh_tools_incremental_uses_args_key(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test that refresh handles 'args' key as alternative to 'inputSchema'."""
        mcp_manager.upsert(
            name="args-refresh",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        stats = mcp_manager.refresh_tools_incremental(
            "args-refresh",
            [{"name": "args_tool", "args": {"type": "object"}}],
            project_id=sample_project["id"],
        )

        assert stats["total"] == 1
        tools = mcp_manager.get_cached_tools("args-refresh", project_id=sample_project["id"])
        assert len(tools) == 1
        assert tools[0].input_schema == {"type": "object"}


class TestMCPServerFromRow:
    """Tests for MCPServer.from_row class method."""

    def test_from_row_with_all_fields(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test from_row with all JSON fields populated."""
        server = mcp_manager.upsert(
            name="full-server",
            transport="stdio",
            command="npx",
            args=["-y", "@test/server"],
            env={"API_KEY": "secret"},
            headers={"X-Auth": "token"},
            description="Full server",
            project_id=sample_project["id"],
        )

        # Verify all fields are properly deserialized
        assert server.args == ["-y", "@test/server"]
        assert server.env == {"API_KEY": "secret"}
        assert server.headers == {"X-Auth": "token"}
        assert server.description == "Full server"
        assert server.enabled is True

    def test_from_row_with_null_json_fields(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test from_row with null JSON fields."""
        server = mcp_manager.upsert(
            name="minimal-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        assert server.args is None
        assert server.env is None
        assert server.headers is None
        assert server.command is None


class TestToolFromRow:
    """Tests for Tool.from_row class method."""

    def test_from_row_with_schema(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test Tool.from_row with input_schema."""
        mcp_manager.upsert(
            name="tool-row-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        schema = {"type": "object", "properties": {"arg1": {"type": "string"}}}
        mcp_manager.cache_tools(
            "tool-row-server",
            [{"name": "schema_tool", "description": "Has schema", "inputSchema": schema}],
            project_id=sample_project["id"],
        )

        tools = mcp_manager.get_cached_tools("tool-row-server", project_id=sample_project["id"])
        assert len(tools) == 1
        assert tools[0].input_schema == schema

    def test_from_row_without_schema(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test Tool.from_row without input_schema."""
        mcp_manager.upsert(
            name="no-schema-row-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        mcp_manager.cache_tools(
            "no-schema-row-server",
            [{"name": "no_schema_tool", "description": "No schema"}],
            project_id=sample_project["id"],
        )

        tools = mcp_manager.get_cached_tools(
            "no-schema-row-server", project_id=sample_project["id"]
        )
        assert len(tools) == 1
        assert tools[0].input_schema is None


class TestMCPServerToConfig:
    """Tests for MCPServer.to_config method edge cases."""

    def test_to_config_minimal(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test to_config with minimal fields."""
        server = mcp_manager.upsert(
            name="minimal-config",
            transport="http",
            project_id=sample_project["id"],
        )

        config = server.to_config()
        assert config["name"] == "minimal-config"
        assert config["transport"] == "http"
        assert config["enabled"] is True
        # Optional fields should not be present
        assert "url" not in config
        assert "command" not in config
        assert "args" not in config
        assert "env" not in config
        assert "headers" not in config
        assert "description" not in config

    def test_to_config_with_project_id(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Test to_config includes project_id when present."""
        server = mcp_manager.upsert(
            name="project-config",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )

        config = server.to_config()
        assert config["project_id"] == sample_project["id"]
