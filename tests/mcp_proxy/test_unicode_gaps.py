"""Untrusted metadata must not bypass Unicode cleaning."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from mcp.types import CallToolResult, ListToolsResult, Tool

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPError, MCPServerConfig
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry
from gobby.skills.loader import GitHubRef, SkillLoader, SkillLoadError
from gobby.skills.parser import ParsedSkill
from gobby.storage.hub.protocol import HubDatabase

HIDDEN = "\u202e\U000e0061"
pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["list", "schema", "cache"])
async def test_external_discovery_cleans_metadata(
    surface: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = MCPServerConfig(
        name="gobby-impostor",
        project_id="test-project",
        transport="http",
        url="https://example.test",
    )
    manager = MCPClientManager(server_configs=[config], project_id="test-project")
    session = SimpleNamespace(
        list_tools=AsyncMock(
            return_value=ListToolsResult(
                tools=[
                    Tool(
                        name="ReadExact",
                        description="description" + HIDDEN,
                        inputSchema={"type": "object", "description": "schema" + HIDDEN},
                    ),
                ]
            )
        )
    )
    monkeypatch.setattr(manager, "get_client_session", AsyncMock(return_value=session))
    internal = InternalRegistryManager()
    registry = InternalToolRegistry("gobby-skills")
    registry.register(
        "owned", "internal" + HIDDEN, {"description": "internal" + HIDDEN}, lambda: {}
    )
    internal.add_registry(registry)
    proxy = ToolProxyService(mcp_manager=manager, internal_manager=internal)
    own = await proxy.get_tool_schema("gobby-skills", "owned")
    assert own["tool"]["description"] == "internal" + HIDDEN
    assert own["tool"]["inputSchema"]["description"] == "internal" + HIDDEN
    if surface == "list":
        result = await proxy.list_tools(config.name, project_id="test-project")
        assert result["tools"] == [{"name": "ReadExact", "brief": "description"}]
    elif surface == "schema":
        result = await proxy.get_tool_schema(config.name, "ReadExact", project_id="test-project")
        assert result["tool"]["inputSchema"]["description"] == "schema"
        assert result["tool"]["description"] == "description"
    else:
        await manager.list_tools(config.id)
        assert config.tools == [{"name": "ReadExact", "brief": "description"}]
        assert manager._tool_schema_cache[config.id][0]["inputSchema"]["description"] == "schema"


@pytest.mark.asyncio
async def test_hidden_structured_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    config = MCPServerConfig(
        name="external", project_id="test-project", transport="http", url="https://example.test"
    )
    manager = MCPClientManager(server_configs=[config], project_id="test-project")
    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=CallToolResult(
                content=[], structuredContent={"key" + HIDDEN: "clean", "key": "other"}
            )
        )
    )
    monkeypatch.setattr(manager, "get_client_session", AsyncMock(return_value=session))
    with pytest.raises(ValueError, match="[Kk]ey"):
        await manager.call_tool(config.id, "read", {})
    session.call_tool.return_value = CallToolResult(content=[], structuredContent={"key": "clean"})
    result = await manager.call_tool(config.id, "read", {})
    assert result.structured_content == {"key": "clean"}


@pytest.mark.parametrize("source", ["zip", "github"])
@pytest.mark.parametrize("hidden_path", [False, True])
def test_imported_fields_and_paths(tmp_path: Path, source: str, hidden_path: bool) -> None:
    root = tmp_path / "sample"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: sample\ndescription: desc\nlicense: MIT"
        + HIDDEN
        + "\nallowed-tools: Read"
        + HIDDEN
        + "\n---\nbody\n",
        encoding="utf-8",
    )
    refs = root / "references"
    refs.mkdir()
    reference = refs / ("guide" + (HIDDEN if hidden_path else "") + ".md")
    reference.write_text("clean content")
    archive = tmp_path / "skill.zip"
    with ZipFile(archive, "w") as zip_file:
        zip_file.write(root / "SKILL.md", "SKILL.md")
        zip_file.write(reference, reference.relative_to(root))

    def load() -> ParsedSkill | list[ParsedSkill]:
        if source == "zip":
            return SkillLoader().load_from_zip(archive, validate=False)
        return SkillLoader()._load_github_checkout(
            GitHubRef(owner="owner", repo="repo", path=None), root, validate=False, load_all=False
        )

    if hidden_path:
        with pytest.raises(SkillLoadError, match="[Pp]ath"):
            load()
    else:
        skill = load()
        assert isinstance(skill, ParsedSkill)
        assert skill.license == "MIT"
        assert skill.allowed_tools == ["Read"]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["list", "schema"])
async def test_external_discovery_errors_are_cleaned(
    surface: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = MCPServerConfig(
        name="external", project_id="test-project", transport="http", url="https://example.test"
    )
    manager = MCPClientManager(server_configs=[config], project_id="test-project")
    session = SimpleNamespace(
        list_tools=AsyncMock(side_effect=RuntimeError("remote failure" + HIDDEN))
    )
    monkeypatch.setattr(manager, "get_client_session", AsyncMock(return_value=session))
    monkeypatch.setattr(manager, "ensure_connected", AsyncMock(return_value=session))
    proxy = ToolProxyService(mcp_manager=manager, internal_manager=InternalRegistryManager())
    if surface == "list":
        result = await proxy.list_tools(config.name, project_id="test-project")
        error = result["error"]
    else:
        with pytest.raises(MCPError) as exc:
            await proxy.get_tool_schema(config.name, "Read", project_id="test-project")
        error = str(exc.value)
    assert "remote failure" in error
    assert "\u202e" not in error
    assert "\U000e0061" not in error


@pytest.mark.asyncio
async def test_hub_errors_are_cleaned(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.config.skills import HubConfig
    from gobby.skills.hubs.manager import HubManager

    manager = HubManager(configs={"remote": HubConfig(type="clawdhub")})
    provider = SimpleNamespace(search=AsyncMock(side_effect=RuntimeError("stderr" + HIDDEN)))
    monkeypatch.setattr(manager, "get_provider", lambda _name: provider)
    results, errors = await manager.search_all("sample")
    assert results == []
    assert errors == {"remote": "stderr"}


@pytest.mark.asyncio
async def test_install_validation_error_is_cleaned(temp_db: HubDatabase, tmp_path: Path) -> None:
    from gobby.mcp_proxy.tools.skills import create_skills_registry

    root = tmp_path / "sample"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: sample\ndescription: desc\nversion: invalid" + HIDDEN + "\n---\nbody\n"
    )
    registry = create_skills_registry(temp_db)
    tool = registry.get_tool("install_skill")
    assert tool is not None
    result = await tool(source=str(root))
    assert result["success"] is False
    assert "invalid" in result["error"]
    assert "\u202e" not in result["error"]
    assert "\U000e0061" not in result["error"]


@pytest.mark.asyncio
async def test_http_discovery_metadata_is_cleaned(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.requests import Request

    from gobby.app_context import ServiceContainer
    from gobby.servers.http import HTTPServer
    from gobby.servers.routes.mcp.endpoints import execution
    from gobby.storage.projects import GLOBAL_PROJECT_ID
    from gobby.storage.tasks import LocalTaskManager

    config = MCPServerConfig(
        name="external", project_id=GLOBAL_PROJECT_ID, transport="http", url="https://example.test"
    )
    manager = MCPClientManager(server_configs=[config], project_id=GLOBAL_PROJECT_ID)
    server = HTTPServer(
        ServiceContainer(
            database=temp_db,
            session_manager=None,
            task_manager=LocalTaskManager(temp_db),
            project_id=GLOBAL_PROJECT_ID,
        ),
        test_mode=True,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/mcp/external/tools",
            "query_string": b"",
            "headers": [(b"x-gobby-project-id", GLOBAL_PROJECT_ID.encode())],
        }
    )
    session = SimpleNamespace(
        list_tools=AsyncMock(
            return_value=ListToolsResult(
                tools=[
                    Tool(
                        name="ReadExact",
                        description="description" + HIDDEN,
                        inputSchema={"type": "object", "description": "schema" + HIDDEN},
                    ),
                ]
            )
        )
    )
    monkeypatch.setattr(manager, "ensure_connected", AsyncMock(return_value=session))
    result = await execution.list_mcp_tools(config.name, request, server, None, manager)
    assert result["success"] is True
    assert result["tools"][0]["description"] == "description"
    assert result["tools"][0]["inputSchema"]["description"] == "schema"
    session.list_tools.side_effect = RuntimeError("remote failure" + HIDDEN)
    failure = await execution.list_mcp_tools(config.name, request, server, None, manager)
    assert failure["success"] is False
    assert failure["error"] == "Failed to list tools: remote failure"
    for response in (result, failure):
        assert "\u202e" not in str(response)
        assert "\U000e0061" not in str(response)


def test_imported_metadata_key_is_rejected() -> None:
    skill = ParsedSkill(
        name="sample",
        description="desc",
        content="body",
        metadata={"key" + HIDDEN: "clean", "key": "other"},
    )
    with pytest.raises(ValueError, match="[Kk]ey"):
        SkillLoader.clean_untrusted_skill(skill)


@pytest.mark.asyncio
async def test_existing_cached_brief_is_cleaned(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging

    from gobby.mcp_proxy.client_manager.server_registry import load_tools_from_db
    from gobby.storage.mcp import LocalMCPManager
    from gobby.storage.projects import GLOBAL_PROJECT_ID

    db = LocalMCPManager(temp_db)
    server = db.insert_server("external", "http", GLOBAL_PROJECT_ID, url="https://example.test")
    assert server is not None
    db.cache_tools(server.id, [{"name": "ReadExact", "description": "cached" + HIDDEN}])
    result = load_tools_from_db(db, server.id, logging.getLogger(__name__))
    assert result == [{"name": "ReadExact", "brief": "cached"}]
    config = MCPServerConfig(
        id=server.id,
        name=server.name,
        project_id=GLOBAL_PROJECT_ID,
        transport="http",
        url="https://example.test",
    )
    manager = MCPClientManager(server_configs=[config], mcp_db_manager=db)
    session = SimpleNamespace(
        list_tools=AsyncMock(
            return_value=ListToolsResult(
                tools=[
                    Tool(
                        name="ReadExact",
                        description="cached" + HIDDEN,
                        inputSchema={"description": "schema" + HIDDEN},
                    ),
                ]
            )
        )
    )
    monkeypatch.setattr(manager, "get_client_session", AsyncMock(return_value=session))
    await manager.list_tools(server.id)
    stored = db.get_cached_tools(server.id)
    assert stored[0].description == "cached"
    assert stored[0].input_schema == {"description": "schema"}
