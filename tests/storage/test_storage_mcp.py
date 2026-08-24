"""Tests for the LocalMCPManager storage layer."""

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.mcp_proxy.bundled import CHROME_DEVTOOLS_NPM_PACKAGE
from gobby.storage.embedding_generation_state import EmbeddingGenerationState
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.projects import GLOBAL_PROJECT_ID, LocalProjectManager
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit

# mcp_servers.id and tools.id are native uuid columns; synthetic row ids must
# be valid UUID strings.
LEGACY_PROJECT_CONTEXT7_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
LEGACY_CHROME_SERVER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2"
LEGACY_CHROME_TOOL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3"
GLOBAL_CHROME_SERVER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa4"
LEGACY_CONTEXT7_SERVER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa5"
LEGACY_CONTEXT7_TOOL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa6"
GLOBAL_CONTEXT7_SERVER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa7"
GLOBAL_CONTEXT7_TOOL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa8"
MIXED_CASE_TOOL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa9"
UNKNOWN_SERVER_ID = "99999999-9999-9999-9999-999999999999"


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
        assert config["env"] == server.env
        assert server.env is not None
        assert SecretStore(mcp_manager.db).resolve(server.env["API_KEY"]) == "secret"
        assert config["requires_oauth"] is False
        assert config["connect_timeout"] == 30.0

    def test_upsert_preserves_explicit_empty_json_fields(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        server = mcp_manager.upsert(
            name="empty-json-server",
            transport="stdio",
            command="node",
            args=[],
            env={},
            headers={},
            project_id=sample_project["id"],
        )

        assert server.args == []
        assert server.env == {}
        assert server.headers == {}

    def test_upsert_rejects_ambiguous_boolean_string(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        with pytest.raises(ValueError, match="enabled must be a boolean"):
            mcp_manager.upsert(
                name="bad-bool-server",
                transport="http",
                url="http://localhost:8080",
                enabled="maybe",  # type: ignore[arg-type]
                project_id=sample_project["id"],
            )


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

    def test_cache_tools_preserves_empty_input_schema(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        mcp_manager.upsert(
            name="empty-schema-server",
            transport="http",
            url="http://localhost:8080",
            project_id=sample_project["id"],
        )

        mcp_manager.cache_tools(
            "empty-schema-server",
            [{"name": "empty_schema", "description": "Empty schema", "inputSchema": {}}],
            project_id=sample_project["id"],
        )

        tools = mcp_manager.get_cached_tools(
            "empty-schema-server",
            project_id=sample_project["id"],
        )
        assert tools[0].input_schema == {}

    def test_cache_tools_skips_empty_and_deduplicates_normalized_names(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        mcp_manager.upsert(
            name="duplicate-tools-server",
            transport="http",
            url="http://localhost:8080",
            project_id=sample_project["id"],
        )

        count = mcp_manager.cache_tools(
            "duplicate-tools-server",
            [
                {"name": "", "inputSchema": {"type": "object"}},
                {"name": "Read_File", "description": "kept", "inputSchema": {"type": "object"}},
                {
                    "name": " read_file ",
                    "description": "duplicate",
                    "inputSchema": {"type": "object"},
                },
            ],
            project_id=sample_project["id"],
        )

        tools = mcp_manager.get_cached_tools(
            "duplicate-tools-server",
            project_id=sample_project["id"],
        )
        assert count == 1
        assert [tool.name for tool in tools] == ["read_file"]
        assert tools[0].description == "kept"


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
        assert server.headers is not None
        assert (
            SecretStore(mcp_manager.db).resolve(server.headers["Authorization"]) == "Bearer token"
        )

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

    def test_upsert_preserves_existing_oauth_and_timeout_when_unspecified(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        server1 = mcp_manager.upsert(
            name="oauth-server",
            transport="http",
            url="http://old-url",
            project_id=sample_project["id"],
            requires_oauth=True,
            oauth_provider="github",
            connect_timeout=45.0,
        )

        server2 = mcp_manager.upsert(
            name="oauth-server",
            transport="http",
            url="http://new-url",
            project_id=sample_project["id"],
        )

        assert server2.id == server1.id
        assert server2.url == "http://new-url"
        assert server2.requires_oauth is True
        assert server2.oauth_provider == "github"
        assert server2.connect_timeout == 45.0

    def test_to_config_preserves_stored_zero_timeout(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_db: HubDatabase,
    ) -> None:
        created = mcp_manager.upsert(
            name="zero-timeout",
            transport="http",
            url="http://localhost:8080",
            project_id=sample_project["id"],
            connect_timeout=1.0,
        )
        temp_db.execute(
            "UPDATE mcp_servers SET connect_timeout = 0 WHERE id = %s",
            (created.id,),
        )

        server = mcp_manager.get_server("zero-timeout", project_id=sample_project["id"])

        assert server is not None
        assert server.connect_timeout == 0.0
        assert server.to_config()["connect_timeout"] == 0.0

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
            "SELECT * FROM mcp_servers WHERE name = %s AND project_id = %s",
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
        mcp_manager.cache_tools(
            "remove-me",
            [{"name": "removed_tool", "inputSchema": {"type": "object"}}],
            project_id=sample_project["id"],
        )
        tool = mcp_manager.get_cached_tools("remove-me", project_id=sample_project["id"])[0]
        generation_state = EmbeddingGenerationState(mcp_manager.db)
        watermark = generation_state.watermark()

        result = mcp_manager.remove_server("remove-me", project_id=sample_project["id"])
        assert result is True
        assert mcp_manager.get_server("remove-me", project_id=sample_project["id"]) is None
        changes = generation_state.changes_after(watermark)
        assert [
            (change.source_kind, change.source_id, change.is_tombstone) for change in changes
        ] == [("tool", tool.id, True)]

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
        result = mcp_manager.get_server_by_id(UNKNOWN_SERVER_ID)
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
        temp_db: HubDatabase,
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
        temp_db.execute(
            """
            INSERT INTO mcp_servers (
                id, name, project_id, transport, command, args, enabled, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                LEGACY_PROJECT_CONTEXT7_ID,
                "context7",
                sample_project["id"],
                "stdio",
                "npx",
                json.dumps(["-y", "@upstash/context7-mcp"]),
                True,
            ),
        )

        project_servers = mcp_manager.list_servers(project_id=sample_project["id"])
        runtime_servers = mcp_manager.list_runtime_servers(project_id=sample_project["id"])

        assert {server.name for server in project_servers} == {"context7", "project-only"}
        runtime_names = {server.name for server in runtime_servers}
        assert runtime_names == {"context7", "project-only"}
        context7_servers = [server for server in runtime_servers if server.name == "context7"]
        assert len(context7_servers) == 1
        assert context7_servers[0].project_id == GLOBAL_PROJECT_ID

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

    def test_cache_tools_with_input_schema_key(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        """Snake-case schemas use the same canonical cache path as MCP SDK schemas."""
        mcp_manager.upsert(
            name="snake-schema-server",
            transport="http",
            url="http://localhost",
            project_id=sample_project["id"],
        )
        input_schema = {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
        }

        mcp_manager.cache_tools(
            "snake-schema-server",
            [{"name": "snake_tool", "input_schema": input_schema}],
            project_id=sample_project["id"],
        )

        tools = mcp_manager.get_cached_tools(
            "snake-schema-server",
            project_id=sample_project["id"],
        )
        assert len(tools) == 1
        assert tools[0].input_schema == input_schema

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

    def test_import_from_mcp_json_non_object_payload(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        temp_dir: Path,
    ) -> None:
        """Test importing from non-object JSON returns 0."""
        mcp_json = temp_dir / ".mcp.json"
        mcp_json.write_text(json.dumps(["not", "an", "object"]))

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
            VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                LEGACY_CHROME_SERVER_ID,
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
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                LEGACY_CHROME_TOOL_ID,
                LEGACY_CHROME_SERVER_ID,
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
            "SELECT * FROM mcp_servers WHERE id = %s",
            (LEGACY_CHROME_SERVER_ID,),
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                GLOBAL_CHROME_SERVER_ID,
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                LEGACY_CONTEXT7_SERVER_ID,
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
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                LEGACY_CONTEXT7_TOOL_ID,
                LEGACY_CONTEXT7_SERVER_ID,
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                GLOBAL_CONTEXT7_SERVER_ID,
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
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                GLOBAL_CONTEXT7_TOOL_ID,
                GLOBAL_CONTEXT7_SERVER_ID,
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
            "SELECT * FROM mcp_servers WHERE id = %s",
            (LEGACY_CONTEXT7_SERVER_ID,),
        )
        assert legacy_row is None


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
        assert server.env is not None
        assert server.headers is not None
        secret_store = SecretStore(mcp_manager.db)
        assert secret_store.resolve(server.env["API_KEY"]) == "secret"
        assert secret_store.resolve(server.headers["X-Auth"]) == "token"
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

    def test_from_row_preserves_predecoded_json_fields(self) -> None:
        from gobby.storage.mcp_models import MCPServer

        server = MCPServer.from_row(
            {
                "id": "server-1",
                "name": "decoded-server",
                "transport": "stdio",
                "url": None,
                "command": "node",
                "args": [],
                "env": {},
                "headers": {"X-Test": "value"},
                "enabled": True,
                "description": None,
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "project_id": "proj-1",
            }
        )

        assert server.args == []
        assert server.env == {}
        assert server.headers == {"X-Test": "value"}

    def test_from_row_malformed_json_includes_server_context(self) -> None:
        """Malformed server JSON reports the row id and field name."""
        from gobby.storage.mcp_models import MCPServer

        with pytest.raises(ValueError, match="MCP server server-1 field args"):
            MCPServer.from_row(
                {
                    "id": "server-1",
                    "name": "bad-server",
                    "transport": "stdio",
                    "url": None,
                    "command": "node",
                    "args": "[",
                    "env": None,
                    "headers": None,
                    "enabled": True,
                    "description": None,
                    "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-01T00:00:00Z",
                    "project_id": "proj-1",
                }
            )


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

    def test_from_row_malformed_input_schema_includes_tool_context(self) -> None:
        """Malformed tool schema JSON reports the tool id and name."""
        from gobby.storage.mcp_models import Tool

        with pytest.raises(ValueError, match=r"MCP tool bad_tool \(tool-1\) input_schema"):
            Tool.from_row(
                {
                    "id": "tool-1",
                    "mcp_server_id": "server-1",
                    "name": "bad_tool",
                    "description": None,
                    "input_schema": "{",
                    "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-01T00:00:00Z",
                }
            )

    def test_from_row_preserves_predecoded_dict_schema(self) -> None:
        from gobby.storage.mcp_models import Tool

        tool = Tool.from_row(
            {
                "id": "tool-1",
                "mcp_server_id": "server-1",
                "name": "decoded_tool",
                "description": None,
                "input_schema": {"type": "object"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            }
        )

        assert tool.input_schema == {"type": "object"}

    def test_from_row_rejects_predecoded_list_schema(self) -> None:
        from gobby.storage.mcp_models import Tool

        with pytest.raises(ValueError, match="expected object, got list"):
            Tool.from_row(
                {
                    "id": "tool-1",
                    "mcp_server_id": "server-1",
                    "name": "decoded_tool",
                    "description": None,
                    "input_schema": [],
                    "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-01T00:00:00Z",
                }
            )


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
        assert config["requires_oauth"] is False
        assert config["connect_timeout"] == 30.0

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


class TestMCPServerSecretPersistence:
    def test_upsert_encrypts_secret_slots_and_keeps_non_secret_values(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        plaintext = "Bearer database-plaintext-token"
        server = mcp_manager.upsert(
            name="secure-server",
            transport="http",
            url="http://localhost",
            env={"API_KEY": "database-api-plaintext", "LOG_LEVEL": "debug"},
            headers={"Authorization": plaintext, "X-Region": "us-east-1"},
            project_id=sample_project["id"],
        )

        assert server.env is not None
        assert server.headers is not None
        assert server.env["API_KEY"].startswith("$secret:mcp_")
        assert server.env["LOG_LEVEL"] == "debug"
        assert server.headers["Authorization"].startswith("$secret:mcp_")
        assert server.headers["X-Region"] == "us-east-1"
        row = mcp_manager.db.fetchone(
            "SELECT env, headers FROM mcp_servers WHERE id = %s",
            (server.id,),
        )
        assert row is not None
        assert "database-api-plaintext" not in str(row["env"])
        assert plaintext not in str(row["headers"])
        store = SecretStore(mcp_manager.db)
        assert store.resolve(server.env["API_KEY"]) == "database-api-plaintext"
        assert store.resolve(server.headers["Authorization"]) == plaintext

    def test_update_reuses_slot_ref_and_cleans_removed_managed_secret(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        created = mcp_manager.upsert(
            name="rotating-server",
            transport="stdio",
            command="node",
            env={"API_KEY": "first-value"},
            headers={"Authorization": "Bearer old-value"},
            project_id=sample_project["id"],
        )
        assert created.env is not None
        assert created.headers is not None
        api_ref = created.env["API_KEY"]
        removed_ref = created.headers["Authorization"]

        updated = mcp_manager.update_server(
            "rotating-server",
            project_id=sample_project["id"],
            env={"API_KEY": "second-value", "MODE": "safe"},
            headers={},
        )

        assert updated is not None
        assert updated.env == {"API_KEY": api_ref, "MODE": "safe"}
        assert updated.headers == {}
        store = SecretStore(mcp_manager.db)
        assert store.resolve(api_ref) == "second-value"
        assert store.get(removed_ref.removeprefix("$secret:")) is None

    def test_explicit_reference_is_preserved_without_reowning_secret(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        store = SecretStore(mcp_manager.db)
        store.set("operator_owned", "operator-value")

        server = mcp_manager.upsert(
            name="explicit-ref-server",
            transport="stdio",
            command="node",
            env={"API_KEY": "$secret:operator_owned", "MODE": "safe"},
            project_id=sample_project["id"],
        )

        assert server.env == {"API_KEY": "$secret:operator_owned", "MODE": "safe"}
        assert store.get("operator_owned") == "operator-value"

    def test_upsert_failure_rolls_back_new_secret(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_execute = mcp_manager.db.execute

        def fail_server_insert(sql: str, params: Any = ()) -> Any:
            if "INSERT INTO mcp_servers" in sql:
                raise RuntimeError("forced server persistence failure")
            return original_execute(sql, params)

        monkeypatch.setattr(mcp_manager.db, "execute", fail_server_insert)
        with pytest.raises(RuntimeError, match="forced server persistence failure"):
            mcp_manager.upsert(
                name="rollback-server",
                transport="stdio",
                command="node",
                env={"API_KEY": "must-rollback"},
                project_id=sample_project["id"],
            )

        assert SecretStore(mcp_manager.db).list() == []

    def test_update_failure_restores_previous_secret_value(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        created = mcp_manager.upsert(
            name="rollback-update-server",
            transport="stdio",
            command="node",
            env={"API_KEY": "original-secret-value"},
            project_id=sample_project["id"],
        )
        assert created.env is not None
        ref = created.env["API_KEY"]
        original_execute = mcp_manager.db.execute

        def fail_server_update(sql: str, params: Any = ()) -> Any:
            if "UPDATE mcp_servers SET" in sql:
                raise RuntimeError("forced server update failure")
            return original_execute(sql, params)

        monkeypatch.setattr(mcp_manager.db, "execute", fail_server_update)
        with pytest.raises(RuntimeError, match="forced server update failure"):
            mcp_manager.update_server(
                "rollback-update-server",
                project_id=sample_project["id"],
                env={"API_KEY": "replacement-secret-value"},
            )

        persisted = mcp_manager.get_server(
            "rollback-update-server",
            project_id=sample_project["id"],
        )
        assert persisted is not None
        assert persisted.env == {"API_KEY": ref}
        assert SecretStore(mcp_manager.db).resolve(ref) == "original-secret-value"

    def test_remove_deletes_owned_secret(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ) -> None:
        server = mcp_manager.upsert(
            name="removed-server",
            transport="stdio",
            command="node",
            env={"PASSWORD": "remove-this-value"},
            project_id=sample_project["id"],
        )
        assert server.env is not None
        secret_name = server.env["PASSWORD"].removeprefix("$secret:")

        assert mcp_manager.remove_server("removed-server", sample_project["id"]) is True
        assert SecretStore(mcp_manager.db).get(secret_name) is None
