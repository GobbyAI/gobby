"""Local MCP server and tool storage manager."""

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp_imports import MCPImportStorageMixin
from gobby.storage.mcp_models import MCPServer, Tool
from gobby.storage.mcp_servers import MCPServerStorageMixin
from gobby.storage.mcp_templates import MCPTemplateStorageMixin
from gobby.storage.mcp_tools import MCPToolStorageMixin

__all__ = ["LocalMCPManager", "MCPServer", "Tool"]


class LocalMCPManager(
    MCPTemplateStorageMixin,
    MCPServerStorageMixin,
    MCPToolStorageMixin,
    MCPImportStorageMixin,
):
    """Manager for local MCP server and tool storage."""

    db: HubDatabase

    def __init__(self, db: HubDatabase) -> None:
        """Initialize with database connection."""
        self.db = db
