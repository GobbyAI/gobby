"""Runner startup ordering and event-loop isolation."""

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, call

import pytest

import gobby.runner_lifecycle as lifecycle
from gobby.app_context import clear_app_context
from gobby.config.bootstrap import BootstrapConfig
from gobby.runner import GobbyRunner, run_gobby
from gobby.runner_pid_file import SupervisedPidOwnership
from gobby.runtime_grants.lease_view import RuntimeLeaseView

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_startup_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("gobby.utils.local_token._daemon_bootstrap", None)
    clear_app_context()
    yield
    clear_app_context()


@pytest.mark.asyncio
@pytest.mark.parametrize("custom_config", [False, True], ids=["default", "custom"])
async def test_startup_provisions_break_glass_before_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, custom_config: bool
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config_path = tmp_path / "custom" / "bootstrap.yaml" if custom_config else None
    credential_home = config_path.parent if config_path is not None else home
    credential_home.mkdir(exist_ok=True)
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.delenv("GOBBY_SUPERVISOR_FD", raising=False)
    database_url = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
    monkeypatch.setattr(
        "gobby.config.bootstrap.load_bootstrap",
        MagicMock(return_value=BootstrapConfig(database_url=database_url)),
    )
    monkeypatch.setattr(
        "gobby.utils.dependency_requirements.unsupported_platform_error", lambda: None
    )
    monkeypatch.setattr("gobby.utils.machine_id.require_machine_id", lambda: "test-machine")
    monkeypatch.setattr("gobby.deployment.deployment_token", lambda: "test-deployment")
    monkeypatch.setattr("gobby.runner_backend.watch_supervisor", AsyncMock())
    monkeypatch.setattr("gobby.providers.version_gate.probe_and_publish_agy_support", AsyncMock())
    monkeypatch.setattr("gobby.runner_init.servers._bind_runtime_grants", MagicMock())

    verify_schema = MagicMock()
    provision_credential = MagicMock()
    lease = MagicMock(spec=RuntimeLeaseView)
    runner = MagicMock(spec=GobbyRunner)
    runner.http_server = MagicMock()
    runner.run = AsyncMock()
    create_runner = AsyncMock(return_value=runner)
    startup = MagicMock()
    startup.attach_mock(verify_schema, "schema")
    startup.attach_mock(provision_credential, "credential")
    startup.attach_mock(lease.bind, "lease")
    startup.attach_mock(create_runner, "create")
    startup.attach_mock(runner.run, "run")
    monkeypatch.setattr("gobby.storage.schema_contract.verify_schema", verify_schema)
    monkeypatch.setattr(
        "gobby.utils.break_glass.ensure_break_glass_credential", provision_credential
    )
    monkeypatch.setattr(
        "gobby.runtime_grants.lease_view.RuntimeLeaseView", MagicMock(return_value=lease)
    )
    monkeypatch.setattr(GobbyRunner, "create", create_runner)
    ownership = SupervisedPidOwnership()

    await run_gobby(config_path=config_path, ownership_resolution=ownership)

    provision_credential.assert_called_once_with(credential_home)
    assert startup.mock_calls == [
        call.schema(database_url),
        call.credential(credential_home),
        call.lease(),
        call.create(config_path=config_path, verbose=False),
        call.run(ownership_resolution=ownership),
    ]
    assert list(credential_home.iterdir()) == []


@pytest.mark.asyncio
async def test_subsystem_startup_does_not_schedule_lag_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = cast(GobbyRunner, object())
    rebuild_vector_store = object()
    loop = asyncio.get_running_loop()
    scheduled_before = tuple(vars(loop)["_scheduled"])
    threads_before = {thread.ident for thread in threading.enumerate()}
    initialized = False

    async def initialize(*args: object, **kwargs: object) -> None:
        nonlocal initialized
        initialized = True

    monkeypatch.setattr(lifecycle, "init_subsystems", initialize)
    await lifecycle._init_subsystems(runner, rebuild_vector_store)

    assert initialized
    assert tuple(vars(loop)["_scheduled"]) == scheduled_before
    assert {thread.ident for thread in threading.enumerate()} == threads_before
