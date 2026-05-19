"""Tests for MCP manager compatibility helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.mcp_proxy.services._manager_compat import manager_server_configs

pytestmark = pytest.mark.unit


def test_manager_server_configs_skips_server_configs_without_string_names() -> None:
    valid_config = SimpleNamespace(name="valid-server", tools=[])
    invalid_name_config = SimpleNamespace(name=123, tools=[])
    missing_name_config = SimpleNamespace(tools=[])
    manager = SimpleNamespace(
        server_configs=[valid_config, invalid_name_config, missing_name_config],
    )

    assert manager_server_configs(manager) == [("valid-server", valid_config)]


def test_manager_server_configs_skips_legacy_configs_without_string_keys() -> None:
    valid_config = SimpleNamespace(tools=[])
    invalid_config = SimpleNamespace(tools=[])
    manager = SimpleNamespace(_configs={"valid-server": valid_config, 123: invalid_config})

    assert manager_server_configs(manager) == [("valid-server", valid_config)]
