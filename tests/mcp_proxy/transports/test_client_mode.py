"""Client negotiation mode selection tests."""

from gobby.mcp_proxy.models import MCPServerConfig
from gobby.mcp_proxy.transports.base import _client_mode_for_config


def _config(*, template: str | None = None) -> MCPServerConfig:
    return MCPServerConfig(
        name="test-server",
        project_id="test-project",
        transport="stdio",
        command="test-server",
        template=template,
    )


def test_openapi_template_uses_legacy_negotiation() -> None:
    assert _client_mode_for_config(_config(template="openapi")) == "legacy"


def test_other_servers_keep_automatic_negotiation() -> None:
    assert _client_mode_for_config(_config()) == "auto"
