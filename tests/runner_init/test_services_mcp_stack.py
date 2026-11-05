"""Tests for MCP stack initialisation via runner_init.mcp_stack."""

from __future__ import annotations

import asyncio
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


_INTERNAL_MANAGER = object()


def _tools_handler(search: Any) -> Any:
    return SimpleNamespace(_semantic_search=search)


async def _run_backfill(
    tools_handler: Any,
    proxy: Any,
    store: Any,
    bootstrap_config: BootstrapConfig | None = None,
) -> set[Any]:
    """Drive the live scheduling with the HTTP server it reads its search and registries from."""
    from gobby.runner_init import mcp_stack

    runner = _fake_runner(
        bootstrap_config=bootstrap_config or BootstrapConfig(),
        mcp_proxy=proxy,
        database=object(),
        http_server=SimpleNamespace(
            _tools_handler=tools_handler, _internal_manager=_INTERNAL_MANAGER
        ),
    )
    before = set(mcp_stack._BACKFILL_TASKS)
    with patch.object(services, "ConfigStore", return_value=store):
        services.schedule_scoped_tool_backfill(runner)
    created = set(mcp_stack._BACKFILL_TASKS) - before
    await asyncio.gather(*created)
    return created


@pytest.mark.asyncio
async def test_scoped_backfill_schedules_with_internal_registries() -> None:
    from gobby.runner_init import mcp_stack

    search = SimpleNamespace(embed_all_tools=AsyncMock(return_value={"embedded": 3}))
    store = _MarkerStore()
    proxy = MagicMock()

    created = await _run_backfill(_tools_handler(search), proxy, store)

    assert len(created) == 1
    search.embed_all_tools.assert_awaited_once_with(
        GLOBAL_PROJECT_ID, proxy, internal_manager=_INTERNAL_MANAGER
    )
    assert store.get(mcp_stack.SCOPED_PAYLOAD_VERSION_KEY) == mcp_stack.SCOPED_PAYLOAD_VERSION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bootstrap_config", "schedules"),
    [
        (BootstrapConfig(), True),
        (BootstrapConfig(hub=True), True),
        (BootstrapConfig(datastore_mode="remote"), False),
    ],
    ids=["standalone", "hub", "node"],
)
async def test_node_mode_skips_scoped_tool_backfill(
    bootstrap_config: BootstrapConfig,
    schedules: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gobby.runner_init import mcp_stack

    search = SimpleNamespace(embed_all_tools=AsyncMock(return_value={"embedded": 3}))
    store = _MarkerStore()

    with caplog.at_level(logging.INFO, logger=services.__name__):
        created = await _run_backfill(_tools_handler(search), MagicMock(), store, bootstrap_config)

    skip = "skipping hub-only scoped tool backfill in node mode"
    if schedules:
        assert len(created) == 1
        search.embed_all_tools.assert_awaited_once()
        assert store.get(mcp_stack.SCOPED_PAYLOAD_VERSION_KEY) == mcp_stack.SCOPED_PAYLOAD_VERSION
        assert skip not in caplog.messages
    else:
        assert created == set()
        search.embed_all_tools.assert_not_awaited()
        assert store.values == {}
        assert caplog.messages.count(skip) == 1


@pytest.mark.asyncio
async def test_scoped_backfill_runs_once_across_restarts() -> None:
    from gobby.runner_init import mcp_stack

    search = SimpleNamespace(embed_all_tools=AsyncMock(return_value={"embedded": 3}))
    store = _MarkerStore()
    tools_handler = _tools_handler(search)

    await _run_backfill(tools_handler, MagicMock(), store)
    await _run_backfill(tools_handler, MagicMock(), store)

    search.embed_all_tools.assert_awaited_once()
    assert store.get(mcp_stack.SCOPED_PAYLOAD_VERSION_KEY) == mcp_stack.SCOPED_PAYLOAD_VERSION


@pytest.mark.asyncio
async def test_scoped_backfill_skips_when_marker_current() -> None:
    from gobby.runner_init import mcp_stack

    search = SimpleNamespace(embed_all_tools=AsyncMock())
    store = _MarkerStore()
    store.set(mcp_stack.SCOPED_PAYLOAD_VERSION_KEY, mcp_stack.SCOPED_PAYLOAD_VERSION)

    await _run_backfill(_tools_handler(search), MagicMock(), store)

    search.embed_all_tools.assert_not_awaited()
    assert store.get(mcp_stack.SCOPED_PAYLOAD_VERSION_KEY) == mcp_stack.SCOPED_PAYLOAD_VERSION


@pytest.mark.asyncio
async def test_scoped_backfill_skipped_without_tools_handler() -> None:
    store = _MarkerStore()

    created = await _run_backfill(None, MagicMock(), store)

    assert created == set()
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
