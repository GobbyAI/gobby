"""Tests for MCP stack initialisation via runner_init.mcp_stack."""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.bootstrap import BootstrapConfig
from gobby.runner import GobbyRunner
from gobby.runner_init import services
from gobby.storage.projects import GLOBAL_PROJECT_ID

pytestmark = pytest.mark.unit


def _fake_runner(**fields: Any) -> GobbyRunner:
    return cast(GobbyRunner, SimpleNamespace(**fields))


def test_init_mcp_stack_refreshes_template_instances() -> None:
    from gobby.runner_init.mcp_stack import init_mcp_stack

    manager = MagicMock()
    manager.refresh_template_instances.return_value = {"refreshed": 2, "errors": {}}
    runner = _fake_runner(
        database=object(),
        startup_config=SimpleNamespace(logging=SimpleNamespace()),
        mcp_db_manager=None,
        mcp_proxy=None,
        metrics_event_store=None,
        metrics_manager=None,
    )
    with (
        patch("gobby.runner_init.mcp_stack.LocalMCPManager", return_value=manager),
        patch("gobby.runner_init.mcp_stack.MCPClientManager") as mock_client,
        patch("gobby.runner_init.mcp_stack.ToolMetricsManager"),
        patch("gobby.runner_init.mcp_stack.MetricsEventStore"),
        patch("gobby.runner_init.mcp_stack.resolved_log_path", return_value="/tmp/gobby.log"),
    ):
        init_mcp_stack(runner)

    manager.refresh_template_instances.assert_called_once()
    assert manager.refresh_template_instances.call_args.args[0] is not None
    assert manager.normalize_bundled_servers.call_count == 0
    mock_client.assert_called_once()
    assert runner.mcp_db_manager is manager
    source = inspect.getsource(services)
    assert "normalize_bundled_servers" not in source
    assert "from gobby.runner_init.mcp_stack import init_mcp_stack" in inspect.getsource(
        services._init_mcp_stack
    )


def test_init_mcp_stack_reports_stale_instance_without_failing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gobby.runner_init.mcp_stack import init_mcp_stack

    manager = MagicMock()
    manager.refresh_template_instances.return_value = {
        "refreshed": 0,
        "errors": {
            "srv-1": {
                "name": "stale",
                "project_id": GLOBAL_PROJECT_ID,
                "error": "required param spec_url is missing",
            }
        },
    }
    runner = _fake_runner(
        database=object(),
        startup_config=SimpleNamespace(logging=SimpleNamespace()),
        mcp_db_manager=None,
        mcp_proxy=None,
        metrics_event_store=None,
        metrics_manager=None,
        degraded_services=set(),
    )
    with (
        patch("gobby.runner_init.mcp_stack.LocalMCPManager", return_value=manager),
        patch("gobby.runner_init.mcp_stack.MCPClientManager") as mock_client,
        patch("gobby.runner_init.mcp_stack.ToolMetricsManager"),
        patch("gobby.runner_init.mcp_stack.MetricsEventStore"),
        patch("gobby.runner_init.mcp_stack.resolved_log_path", return_value="/tmp/gobby.log"),
        caplog.at_level(logging.WARNING),
    ):
        init_mcp_stack(runner)

    mock_client.assert_called_once()
    assert runner.mcp_proxy is mock_client.return_value
    assert "stale" in caplog.text
    assert GLOBAL_PROJECT_ID in caplog.text
    assert "gobby mcp-proxy add-server --template" in caplog.text
    assert "gobby secrets set" in caplog.text


class _MarkerStore:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value


@pytest.mark.asyncio
async def test_stateful_init_backfill_skipped_without_memory_bundle() -> None:
    from gobby.runner_init import mcp_stack

    runner = _fake_runner(
        bootstrap_config=BootstrapConfig(),
        config_runtime=SimpleNamespace(capture=lambda: SimpleNamespace(services={})),
        mcp_proxy=MagicMock(),
        database=object(),
        definition_revision_listener=None,
    )
    store = _MarkerStore()
    before = set(mcp_stack._BACKFILL_TASKS)
    with (
        patch.object(services, "_init_stateful_dependencies") as deps,
        patch.object(services, "_register_stateful_services", new=AsyncMock()) as register,
        patch.object(services, "_apply_stateful_services") as apply_services,
        patch.object(services, "_init_project_context"),
        patch.object(services, "ConfigStore", return_value=store),
    ):
        await services.init_stateful_services(runner)
    deps.assert_called_once_with(runner)
    register.assert_awaited_once_with(runner)
    apply_services.assert_called_once_with(runner)
    assert set(mcp_stack._BACKFILL_TASKS) == before
    assert store.values == {}


def test_scoped_backfill_marker_roundtrip_real_config_store(temp_db: Any) -> None:
    from gobby.runner_init.mcp_stack import (
        SCOPED_PAYLOAD_VERSION_KEY,
        _store_get,
        _store_set,
    )
    from gobby.storage.config_store import ConfigStore

    store = ConfigStore(temp_db)
    assert _store_get(store, SCOPED_PAYLOAD_VERSION_KEY) is None
    _store_set(store, SCOPED_PAYLOAD_VERSION_KEY, 1)
    assert _store_get(store, SCOPED_PAYLOAD_VERSION_KEY) == 1
