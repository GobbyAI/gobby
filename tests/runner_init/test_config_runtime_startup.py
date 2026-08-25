from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import gobby.runner_lifecycle_processes as runner_lifecycle_processes
from gobby.app_context import ServiceContainer, clear_app_context, get_app_context, set_app_context
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.runtime import ConfigRuntime
from gobby.runner import GobbyRunner
from gobby.shutdown_intent import ShutdownIntent


class _Runtime:
    def __init__(
        self,
        events: list[str],
        *,
        start_hook: Callable[[], Awaitable[None]] | None = None,
        start_error: Exception | None = None,
        start_degraded: bool = False,
    ) -> None:
        self.events = events
        self.start_hook = start_hook
        self.start_error = start_error
        self.publisher: Callable[[int], Awaitable[None]] | None = None
        self.ready = False
        self.degraded = start_degraded
        self.closed = False

    async def start(self) -> SimpleNamespace:
        self.events.append("runtime.start")
        if self.start_hook is not None:
            await self.start_hook()
        if self.start_error is not None:
            raise self.start_error
        self.ready = True
        return SimpleNamespace(active=DaemonConfig(ui={"enabled": False}))

    def register_revision_publisher(
        self,
        publisher: Callable[[int], Awaitable[None]],
    ) -> None:
        self.publisher = publisher

    async def reconcile(self, revision: int) -> None:
        if self.publisher is not None:
            await self.publisher(revision)
        self.degraded = False

    async def close(self) -> None:
        self.events.append("runtime.close")
        self.closed = True
        self.ready = False


def _patch_runner_phases(
    monkeypatch: pytest.MonkeyPatch,
    runtime: _Runtime,
    events: list[str],
) -> None:
    def storage(runner: GobbyRunner, _path: object, _verbose: bool) -> None:
        events.append("storage")
        runner.startup_config = DaemonConfig()
        runner.bootstrap_config = BootstrapConfig()
        runner.config_runtime = cast(ConfigRuntime, runtime)

    async def services(_runner: GobbyRunner) -> None:
        events.append("services")

    def capacity(_runner: GobbyRunner) -> None:
        events.append("capacity")

    def orchestration(_runner: GobbyRunner, _config: DaemonConfig) -> None:
        events.append("orchestration")

    def servers(runner: GobbyRunner) -> None:
        events.append("servers")
        container = ServiceContainer(
            database=MagicMock(),
            session_manager=None,
            task_manager=MagicMock(),
            config_runtime=runner.config_runtime,
        )
        set_app_context(container)
        runner.http_server = cast(Any, SimpleNamespace(services=container))

    monkeypatch.setattr("gobby.runner_init.init_storage_and_config", storage)
    monkeypatch.setattr("gobby.runner_init.init_runtime_capacity", capacity)
    monkeypatch.setattr("gobby.runner_init.services.init_stateful_services", services)
    monkeypatch.setattr("gobby.runner_init.init_orchestration", orchestration)
    monkeypatch.setattr("gobby.runner_init.init_servers", servers)


@pytest.fixture(autouse=True)
def _clear_context() -> Iterator[None]:
    clear_app_context()
    yield
    clear_app_context()


async def test_startup_constructs_one_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    runtime = _Runtime(events)
    _patch_runner_phases(monkeypatch, runtime, events)

    runner = await GobbyRunner.create()

    assert runner.config_runtime is cast(ConfigRuntime, runtime)
    assert events == [
        "storage",
        "runtime.start",
        "capacity",
        "services",
        "orchestration",
        "servers",
    ]


async def test_context_shares_runner_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    runtime = _Runtime(events)
    _patch_runner_phases(monkeypatch, runtime, events)

    runner = await GobbyRunner.create()

    context = get_app_context()
    assert context is not None
    assert context.config_runtime is runner.config_runtime


async def test_runtime_closes_with_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby import runner_lifecycle_shutdown as shutdown

    events: list[str] = []
    runtime = _Runtime(events)
    runner = SimpleNamespace(config_runtime=runtime)
    monkeypatch.setattr(shutdown, "_settle_terminal_delivery_barrier", AsyncMock())
    monkeypatch.setattr(shutdown, "_shutdown_database_concurrency", AsyncMock())
    monkeypatch.setattr(
        runner_lifecycle_processes,
        "_preserved_agent_terminal_pids",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        "gobby.telemetry.rule_allow_audit.shutdown_rule_allow_audit",
        AsyncMock(),
    )
    reap = AsyncMock()

    await shutdown._run_async_shutdown_cleanup(
        cast(GobbyRunner, runner),
        shutdown_intent=ShutdownIntent.STOP,
        reap_remaining_child_processes=reap,
        shutdown_telemetry=MagicMock(),
    )

    assert runtime.closed is True
    assert events == ["runtime.close"]


async def test_startup_registers_config_event_publisher() -> None:
    from gobby.runner_init.servers import register_config_event_publisher

    events: list[str] = []
    runtime = _Runtime(events)
    published_revisions: list[int] = []

    async def publish_revision(revision: int) -> None:
        published_revisions.append(revision)

    websocket = SimpleNamespace(broadcast_config_event=publish_revision)
    endpoint_health = SimpleNamespace(configuration_changed=MagicMock())
    http_server = SimpleNamespace(
        services=SimpleNamespace(generation_endpoint_health=endpoint_health)
    )
    runner = SimpleNamespace(
        config_runtime=runtime,
        websocket_server=websocket,
        http_server=http_server,
    )

    register_config_event_publisher(cast(GobbyRunner, runner))
    await runtime.reconcile(17)

    assert published_revisions == [17]
    endpoint_health.configuration_changed.assert_called_once_with()


async def test_startup_closes_subscription_window(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    subscription_active = asyncio.Event()
    release_reload = asyncio.Event()

    async def initial_load() -> None:
        events.append("subscription.active")
        subscription_active.set()
        await release_reload.wait()
        events.append("initial.reload")

    runtime = _Runtime(events, start_hook=initial_load)
    _patch_runner_phases(monkeypatch, runtime, events)

    startup = asyncio.create_task(GobbyRunner.create())
    await subscription_active.wait()
    events.append("revision.committed")
    release_reload.set()
    await startup

    assert events.index("revision.committed") < events.index("initial.reload")
    assert events.index("initial.reload") < events.index("services")


async def test_first_start_failure_and_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    required = _Runtime(events, start_error=RuntimeError("required subscriber failed"))
    _patch_runner_phases(monkeypatch, required, events)

    with pytest.raises(RuntimeError, match="required subscriber failed"):
        await GobbyRunner.create()

    assert "services" not in events
    assert required.ready is False
    assert required.closed is True

    optional = _Runtime(events, start_degraded=True)
    _patch_runner_phases(monkeypatch, optional, events)
    await GobbyRunner.create()
    assert optional.degraded is True

    await optional.reconcile(2)
    assert optional.degraded is False
