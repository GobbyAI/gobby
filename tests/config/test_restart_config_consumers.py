from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.app_context import ServiceContainer
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.registry import CONFIG_REGISTRY, ActivationPolicy, UnknownConfigKeyError
from gobby.config.runtime import ConfigRuntime, ConfigSnapshotRepository
from gobby.runner import GobbyRunner
from gobby.runner_init.servers import init_servers
from gobby.servers import _app_ui
from gobby.servers.http import HTTPServer
from gobby.storage.concurrency import PostgresCapacity
from gobby.storage.hub.protocol import HubDatabase


@dataclass(frozen=True)
class _StoredSnapshot:
    revision: int
    values: MappingProxyType[str, object]
    overrides: MappingProxyType[str, object]
    row_revisions: MappingProxyType[str, int]
    secret_bindings: MappingProxyType[str, object]


class _Repository:
    def __init__(self, snapshots: list[_StoredSnapshot]) -> None:
        self.snapshots = snapshots
        self.index = 0

    def read(self, *, resolve_secrets: bool = True) -> _StoredSnapshot:
        assert resolve_secrets
        return self.snapshots[self.index]

    def read_bounded(
        self,
        *,
        resolve_secrets: bool = True,
        statement_timeout_ms: int,
        lock_timeout_ms: int,
    ) -> _StoredSnapshot:
        assert statement_timeout_ms > 0
        assert lock_timeout_ms > 0
        return self.read(resolve_secrets=resolve_secrets)

    def runtime_candidate(
        self, overrides: dict[str, object], _secret_bindings: object
    ) -> DaemonConfig:
        config = DaemonConfig(ui={"enabled": False})
        return config.model_copy(update={"test_mode": bool(overrides.get("test_mode", False))})


@dataclass(frozen=True)
class _Spec:
    activation: ActivationPolicy


class _Registry:
    def resolve(self, key: str) -> _Spec:
        assert key == "test_mode"
        return _Spec(ActivationPolicy.RESTART_REQUIRED)


def _stored(revision: int, *, test_mode: bool) -> _StoredSnapshot:
    values = MappingProxyType({"test_mode": test_mode})
    return _StoredSnapshot(
        revision=revision,
        values=values,
        overrides=values,
        row_revisions=MappingProxyType({"test_mode": revision}),
        secret_bindings=MappingProxyType({}),
    )


def _services(database: HubDatabase) -> ServiceContainer:
    return ServiceContainer(
        database=database,
        session_manager=MagicMock(),
        task_manager=MagicMock(),
        text_generation_service=MagicMock(),
        tool_chat_service=MagicMock(),
        llm_service=MagicMock(),
    )


def test_topology_uses_bootstrap_only() -> None:
    server_source = inspect.getsource(init_servers)
    ui_source = inspect.getsource(_app_ui)

    assert "runner.bootstrap_config.daemon_port" in server_source
    assert "runner.bootstrap_config.bind_host" in server_source
    assert "runner.bootstrap_config.websocket_port" in server_source
    assert "runner.config.daemon_port" not in server_source
    assert "runner.config.bind_host" not in server_source
    assert "runner.config.websocket.port" not in server_source
    assert "server.bootstrap_config.ui_port" in ui_source
    assert "config.ui.port" not in ui_source


@pytest.mark.asyncio
async def test_restart_changes_remain_pending() -> None:
    repository = _Repository([_stored(0, test_mode=False), _stored(1, test_mode=True)])
    runtime = ConfigRuntime(cast(ConfigSnapshotRepository, repository), registry=_Registry())
    initial = await runtime.start()
    server = HTTPServer(
        services=_services(cast(HubDatabase, MagicMock())),
        test_mode=initial.active.test_mode,
        bootstrap_config=BootstrapConfig(auth_mode="disabled"),
        startup_config=initial.active,
    )
    middleware_before = tuple(server.app.user_middleware)

    repository.index = 1
    changed = await runtime.reconcile_revision(1)

    assert changed.desired.test_mode is True
    assert changed.active.test_mode is False
    assert changed.pending_restart_keys == frozenset({"test_mode"})
    assert server.startup_config is not None
    assert server.startup_config.test_mode is False
    assert tuple(server.app.user_middleware) == middleware_before
    await runtime.close()


@pytest.mark.asyncio
async def test_restart_promotes_desired_to_active(monkeypatch: pytest.MonkeyPatch) -> None:
    desired = DaemonConfig(test_mode=False, ui={"enabled": False})
    active = DaemonConfig(test_mode=True, ui={"enabled": False})
    runtime = MagicMock()
    runtime.start = MagicMock(return_value=None)

    async def start() -> SimpleNamespace:
        return SimpleNamespace(active=active)

    runtime.start = start
    runtime.close = MagicMock()

    async def close() -> None:
        return None

    runtime.close = close

    def initialize_storage(
        runner: GobbyRunner,
        _config_path: object,
        _verbose: bool,
    ) -> None:
        runner.startup_config = desired
        runner.config_runtime = cast(ConfigRuntime, runtime)
        runner.bootstrap_config = BootstrapConfig()

    observed: list[DaemonConfig] = []
    monkeypatch.setattr(GobbyRunner, "_initialize_storage", initialize_storage)

    async def initialize_runtime_services(runner: GobbyRunner) -> None:
        observed.append(runner.startup_config)

    monkeypatch.setattr(GobbyRunner, "_initialize_runtime_services", initialize_runtime_services)

    runner = await GobbyRunner.create()

    # create() promotes the runtime's ACTIVE snapshot (with bootstrap-owned
    # fields overlaid) over the stored desired projection.
    assert runner.startup_config.test_mode is active.test_mode
    assert runner.startup_config.test_mode is not desired.test_mode
    assert observed == [runner.startup_config]


def test_auth_mode_is_bootstrap_owned() -> None:
    with pytest.raises(UnknownConfigKeyError):
        CONFIG_REGISTRY.resolve("auth_mode")

    startup_config = DaemonConfig(auth_mode="required", ui={"enabled": False})
    services = _services(cast(HubDatabase, MagicMock()))
    server = HTTPServer(
        services=services,
        bootstrap_config=BootstrapConfig(auth_mode="disabled"),
        startup_config=startup_config,
    )

    assert server.auth_service.enabled is False
    constructor_source = inspect.getsource(HTTPServer.__init__)
    assert "services.config" not in constructor_source.split("effective_auth_mode", 1)[0]
    assert "auth_mode:" not in constructor_source


def test_two_stage_pool_and_executor_sizing() -> None:
    from gobby.runner_init.storage import init_runtime_capacity

    active = DaemonConfig(
        database_concurrency={
            "pool_max_size": 40,
            "executor_max_workers": 12,
            "coverage_max_concurrency": 3,
        }
    )
    database = MagicMock()
    database.server_capacity.return_value = PostgresCapacity(
        max_connections=200,
        superuser_reserved_connections=3,
        reserved_connections=0,
    )
    runner = SimpleNamespace(
        startup_config=active,
        database=database,
        verbose=False,
    )

    with (
        patch("gobby.runner_init.storage.DatabaseExecutor") as database_executor,
        patch("gobby.runner_init.storage.CoverageExecutor") as coverage_executor,
        patch("gobby.runner_init.storage.WorktreeDeleteExecutor") as delete_executor,
        patch("gobby.runner_init.storage.DatabaseSaturationWatchdog") as watchdog,
        patch("gobby.runner_init.storage.init_telemetry") as init_telemetry,
    ):
        init_runtime_capacity(cast(Any, runner))

    assert runner.database_concurrency.pool_max_size == 40
    assert runner.database_concurrency.executor_max_workers == 12
    assert runner.database_concurrency.coverage_max_concurrency == 3
    assert runner.db_executor is database_executor.return_value
    assert runner.coverage_executor is coverage_executor.return_value
    assert runner.worktree_delete_executor is delete_executor.return_value
    assert runner.database_watchdog is watchdog.return_value
    database.resize_pool.assert_called_once_with(40)
    database_executor.assert_called_once_with(max_workers=12)
    coverage_executor.assert_called_once_with(max_concurrency=3)
    delete_executor.assert_called_once_with(max_workers=4)
    watchdog.return_value.start.assert_called_once_with()
    init_telemetry.assert_called_once_with(active.telemetry, active.logging, verbose=False)
