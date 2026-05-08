from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.registries import setup_internal_registries

pytestmark = pytest.mark.unit


def test_merge_server_registration_integration() -> None:
    """Verify that gobby-merge server is correctly registered and reachable."""
    # Setup dependencies
    merge_storage = MagicMock()
    merge_resolver = MagicMock()

    # Setup registries with merge components
    manager = setup_internal_registries(
        _config=MagicMock(),
        merge_storage=merge_storage,
        merge_resolver=merge_resolver,
    )

    # 1. Verify server listing
    servers = manager.list_servers()
    merge_server = next((s for s in servers if s["name"] == "gobby-merge"), None)
    assert merge_server is not None, "gobby-merge server not found in list_servers()"

    # 2. Verify tool routing
    # Check that merge tools map to the correct server
    tools_to_check = [
        "merge_start",
        "merge_status",
        "merge_resolve",
        "merge_apply",
        "merge_abort",
    ]

    for tool_name in tools_to_check:
        server_name = manager.find_tool_server(tool_name)
        assert server_name == "gobby-merge", (
            f"Tool {tool_name} should route to gobby-merge, got {server_name}"
        )

    # 3. Verify registry retrieval
    registry = manager.get_registry("gobby-merge")
    assert registry is not None
    assert len(registry) >= len(tools_to_check)
