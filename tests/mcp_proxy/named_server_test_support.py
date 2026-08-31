"""Attach named MCPServerConfig rows onto MagicMock managers for 4.2 resolution."""

from __future__ import annotations

from unittest.mock import MagicMock

from gobby.mcp_proxy.models import MCPServerConfig
from gobby.storage.projects import GLOBAL_PROJECT_ID


def attach_named_servers(
    manager: MagicMock,
    *names: str,
    project_id: str | None = None,
) -> None:
    scope = project_id or GLOBAL_PROJECT_ID
    configs = [
        MCPServerConfig(
            name=name,
            project_id=scope,
            url="https://example.test",
            id=name,
        )
        for name in names
    ]
    manager.server_configs = configs
    manager._configs = {config.id: config for config in configs}
    manager.get_server_config.side_effect = lambda sid: manager._configs.get(sid)
    manager.has_server.side_effect = lambda sid: sid in manager._configs
