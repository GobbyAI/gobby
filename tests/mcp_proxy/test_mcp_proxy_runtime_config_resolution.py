"""Integration coverage for runtime configuration resolution across MCP calls."""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import MagicMock

import pytest

from gobby.app_context import ServiceContainer
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.runtime import ConfigRuntime, RuntimeActiveBundle
from gobby.config.runtime_models import ConfigSnapshot
from gobby.config.values import ConfigValuesService
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.tools.config import create_config_registry
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry
from gobby.servers.http import HTTPServer
from gobby.storage.config_mutations import ConfigMutationResult, ConfigPatch
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit

_SESSION_ID = "796c13d5-34bd-4b6a-b60c-b022df873ad2"
_CONFIG_KEY = "chat_history.max_message_chars"


class _MutableRuntime(ConfigRuntime):
    def __init__(self, snapshot: ConfigSnapshot, *, ready: bool = True) -> None:
        self.current = snapshot
        self.reconciled = snapshot
        self.is_ready = ready
        self.capture_count = 0

    @property
    def ready(self) -> bool:
        return self.is_ready

    @property
    def snapshot(self) -> ConfigSnapshot:
        return self.current

    def capture(self) -> RuntimeActiveBundle:
        self.capture_count += 1
        return RuntimeActiveBundle(snapshot=self.current, services=MappingProxyType({}))

    async def reconcile_local_commit(self, _revision: int) -> ConfigSnapshot:
        self.current = self.reconciled
        return self.current


class _MutationWriter:
    def __init__(self, revision: int) -> None:
        self.revision = revision

    def patch(self, *, expected_revision: int, patch: ConfigPatch) -> ConfigMutationResult:
        del expected_revision, patch
        return ConfigMutationResult(self.revision, frozenset({_CONFIG_KEY}))


class _MemoryManager:
    def __init__(self, db: HubDatabase) -> None:
        self.db = db


def _snapshot(revision: int, max_chars: int) -> ConfigSnapshot:
    config = DaemonConfig.model_validate({"chat_history": {"max_message_chars": max_chars}})
    return ConfigSnapshot(
        revision=revision,
        desired=config,
        active=config,
        row_revisions={_CONFIG_KEY: revision},
        pending_restart_keys=frozenset(),
        failed_live_keys={},
        desired_values={_CONFIG_KEY: max_chars},
        active_values={_CONFIG_KEY: max_chars},
    )


def _proxy(
    manager: InternalRegistryManager,
    server: HTTPServer | None = None,
) -> ToolProxyService:
    mcp_manager = MagicMock()
    mcp_manager.project_id = None
    mcp_manager.session_manager = None
    return ToolProxyService(
        mcp_manager=mcp_manager,
        internal_manager=manager,
        validate_arguments=False,
        operation_context_factory=(server.capture_runtime_operation if server else None),
    )


def _http_server(
    startup_config: DaemonConfig | None = None,
    *,
    bootstrap_config: BootstrapConfig | None = None,
) -> HTTPServer:
    return HTTPServer(
        services=ServiceContainer(
            database=MagicMock(),
            session_manager=None,
            task_manager=MagicMock(),
        ),
        startup_config=startup_config,
        test_mode=True,
        bootstrap_config=bootstrap_config or BootstrapConfig(),
    )


def test_http_config_reuses_bootstrap_overlaid_projection_per_epoch() -> None:
    runtime = _MutableRuntime(_snapshot(1, 3))
    server = _http_server(
        runtime.snapshot.active,
        bootstrap_config=BootstrapConfig(daemon_port=62000),
    )
    server.services.config_runtime = runtime

    first = server.config
    second = server.config

    assert first is not None
    assert second is not None
    assert first is second
    assert first.daemon_port == 62000


def _epoch_probe(
    server: HTTPServer,
    runtime: _MutableRuntime,
    next_snapshot: ConfigSnapshot,
    *,
    activate_runtime: bool = False,
) -> InternalRegistryManager:
    registry = InternalToolRegistry("gobby-epoch")

    @registry.tool(name="probe_epoch", description="Read one runtime epoch twice.")
    async def probe_epoch() -> list[int]:
        first = server.resolve_runtime_config()
        runtime.current = next_snapshot
        if activate_runtime:
            runtime.is_ready = True
        second = server.resolve_runtime_config()
        assert first is not None
        assert second is not None
        return [first.chat_history.max_message_chars, second.chat_history.max_message_chars]

    manager = InternalRegistryManager()
    manager.add_registry(registry)
    return manager


@pytest.mark.asyncio
async def test_in_process_mcp_call_pins_one_active_runtime_epoch() -> None:
    first_snapshot = _snapshot(1, 3)
    second_snapshot = _snapshot(2, 17)
    runtime = _MutableRuntime(first_snapshot)
    http_server = _http_server(first_snapshot.active)
    http_server.services.config_runtime = runtime
    manager = _epoch_probe(http_server, runtime, second_snapshot)

    result = await _proxy(manager, http_server).call_tool("gobby-epoch", "probe_epoch", {})

    assert result == [3, 3]
    assert runtime.capture_count == 1
    current = http_server.resolve_runtime_config()
    assert current is not None
    assert current.chat_history.max_message_chars == 17


@pytest.mark.asyncio
async def test_in_process_mcp_call_pins_pre_start_fallback() -> None:
    startup_config = DaemonConfig.model_validate({"chat_history": {"max_message_chars": 3}})
    runtime = _MutableRuntime(_snapshot(2, 17), ready=False)
    http_server = _http_server(startup_config)
    http_server.services.config_runtime = runtime
    manager = _epoch_probe(
        http_server,
        runtime,
        runtime.snapshot,
        activate_runtime=True,
    )

    result = await _proxy(manager, http_server).call_tool("gobby-epoch", "probe_epoch", {})

    assert result == [3, 3]
    assert runtime.capture_count == 0
    current = http_server.resolve_runtime_config()
    assert current is not None
    assert current.chat_history.max_message_chars == 17


@pytest.mark.asyncio
async def test_live_config_patch_changes_next_mcp_call(temp_db: HubDatabase) -> None:
    del temp_db
    initial = _snapshot(1, 3)
    runtime = _MutableRuntime(initial)
    runtime.reconciled = _snapshot(2, 17)
    config_service = ConfigValuesService(
        runtime=runtime,
        mutations=_MutationWriter(revision=2),
    )
    observed: list[int] = []
    probe = InternalToolRegistry("gobby-probe")

    @probe.tool(name="read_max_chars", description="Record the chat-history char budget in force.")
    async def read_max_chars() -> int:
        value = runtime.snapshot.active.chat_history.max_message_chars
        observed.append(value)
        return value

    manager = InternalRegistryManager()
    manager.add_registry(create_config_registry(lambda: config_service))
    manager.add_registry(probe)
    proxy = _proxy(manager)

    with session_context_for_test(_SESSION_ID):
        await proxy.call_tool("gobby-probe", "read_max_chars", {})
        patch_result = await proxy.call_tool(
            "gobby-config",
            "patch_config_values",
            {
                "expected_revision": 1,
                "values": {"chat_history": {"max_message_chars": 17}},
            },
        )
        await proxy.call_tool("gobby-probe", "read_max_chars", {})

    assert patch_result["committed"] is True
    assert patch_result["apply_status"] == "applied"
    assert observed == [3, 17]
