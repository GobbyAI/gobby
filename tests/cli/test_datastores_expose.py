"""Hub datastore exposure and lifecycle serialization contracts."""

from __future__ import annotations

import shutil
import subprocess
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import pytest
from click.testing import CliRunner

import gobby.cli.datastores as datastores
import gobby.cli.installers.managed_services_lock as managed_services_lock_module
from gobby.cli import cli, daemon
from gobby.cli._daemon_services import ServiceStartResult
from gobby.cli.installers.compose_env import ComposeRuntime
from gobby.cli.installers.managed_services_lock import managed_services_lock
from gobby.config.bootstrap_io import read_bootstrap_yaml, write_bootstrap_yaml
from gobby.storage.config_mutations import ConfigPatch

pytestmark = pytest.mark.unit


def _write_local_bootstrap(home: Path, bind_address: str = "127.0.0.1") -> None:
    files_home = home / "files"
    files_home.mkdir(exist_ok=True)
    write_bootstrap_yaml(
        home / "bootstrap.yaml",
        {
            "datastore_mode": "local",
            "files_home": str(files_home),
            "database_url": "postgresql://gobby:secret@localhost:60891/gobby",
            "services_bind_address": bind_address,
        },
    )


def _patch_successful_exposure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(datastores, "_tailscale_ipv4_addresses", lambda: {"100.64.0.7"})
    monkeypatch.setattr(datastores, "_snapshot_compose_running", lambda _home: True)
    monkeypatch.setattr(datastores, "_start_managed_services", lambda _home: (True, "ready"))


def test_expose_sets_keys_and_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_local_bootstrap(tmp_path)
    _patch_successful_exposure(monkeypatch)
    values: dict[str, Any] = {"databases.qdrant.port": 6333}

    class _Store:
        def __init__(self, _database: object) -> None:
            pass

        def read_snapshot(self) -> SimpleNamespace:
            return SimpleNamespace(revision=0, values=values, overrides=values)

        def patch(self, *, expected_revision: int, patch: ConfigPatch) -> None:
            assert expected_revision == 0
            values.update(patch.values)
            for key in patch.unset:
                values.pop(key, None)

    monkeypatch.setattr(
        "gobby.storage.hub.runtime.runtime_hub_database",
        lambda *_args, **_kwargs: nullcontext(object()),
    )
    monkeypatch.setattr("gobby.storage.config_store.ConfigStore", _Store)

    first = datastores.expose_datastores(
        tmp_path,
        bind_address="100.64.0.7",
        published_host="hub.tailnet.ts.net",
    )
    second = datastores.expose_datastores(
        tmp_path,
        bind_address="100.64.0.7",
        published_host="hub.tailnet.ts.net",
    )

    assert first == second
    assert read_bootstrap_yaml(tmp_path / "bootstrap.yaml")["services_bind_address"] == (
        "100.64.0.7"
    )
    assert values["databases.published_host"] == "hub.tailnet.ts.net"
    assert values["databases.qdrant.url"] == "http://hub.tailnet.ts.net:6333"
    assert values["databases.falkordb.host"] == "hub.tailnet.ts.net"


def test_expose_registered_at_root() -> None:
    result = CliRunner().invoke(cli, ["datastores", "expose", "--help"])

    assert result.exit_code == 0, result.output
    assert "--bind" in result.output
    assert "--host" in result.output


def test_cold_start_reads_bind_from_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_local_bootstrap(tmp_path)
    _patch_successful_exposure(monkeypatch)
    monkeypatch.setattr(datastores, "_commit_shared_endpoints", lambda _home, _host: None)
    datastores.expose_datastores(
        tmp_path,
        bind_address="100.64.0.7",
        published_host="hub.tailnet.ts.net",
    )

    services = tmp_path / "services"
    services.mkdir()
    (services / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        daemon,
        "resolve_compose_runtime",
        lambda _home, **_kwargs: ComposeRuntime(environment={}, profiles=("postgres",)),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    assert daemon._services_stop(tmp_path) is True

    order: list[str] = []
    monkeypatch.setattr(daemon, "get_gobby_home", lambda: tmp_path)
    monkeypatch.setattr("gobby.cli.get_gobby_home", lambda: tmp_path)
    monkeypatch.setattr(daemon, "_start_dependency_errors", lambda: [])

    def start_services(home: Path) -> ServiceStartResult:
        bind = read_bootstrap_yaml(home / "bootstrap.yaml")["services_bind_address"]
        order.append(f"services:{bind}")
        return ServiceStartResult("success", "ready")

    class _Runtime:
        @property
        def operational_config(self) -> object:
            order.append("config")
            raise click.ClickException("stop after sequencing check")

        @property
        def config(self) -> object:
            return self.operational_config

    monkeypatch.setattr(daemon, "_services_start", start_services)
    monkeypatch.setattr("gobby.cli.runtime.get_cli_runtime", lambda _ctx: _Runtime())

    result = CliRunner().invoke(cli, ["start"])

    assert result.exit_code != 0
    assert order == ["services:100.64.0.7", "config"]


def test_expose_failure_restores_prior_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_local_bootstrap(tmp_path, "127.0.0.1")
    original = read_bootstrap_yaml(tmp_path / "bootstrap.yaml")
    monkeypatch.setattr(datastores, "_tailscale_ipv4_addresses", lambda: {"100.64.0.7"})
    monkeypatch.setattr(datastores, "_snapshot_compose_running", lambda _home: True)
    outcomes = iter([(False, "compose failed"), (True, "restored")])
    monkeypatch.setattr(datastores, "_start_managed_services", lambda _home: next(outcomes))
    monkeypatch.setattr(
        datastores,
        "_commit_shared_endpoints",
        lambda *_args: pytest.fail("shared endpoints changed before health succeeded"),
    )

    with pytest.raises(datastores.DatastoreExposureError, match="compose failed"):
        datastores.expose_datastores(
            tmp_path,
            bind_address="100.64.0.7",
            published_host="hub.tailnet.ts.net",
        )

    assert read_bootstrap_yaml(tmp_path / "bootstrap.yaml") == original


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("127.0.0.1", True),
        ("127.12.0.4", True),
        ("100.64.0.7", True),
        ("0.0.0.0", False),
        ("192.168.1.20", False),
        ("::1", False),
        ("fd7a:115c:a1e0::1", False),
        ("::", False),
    ],
)
def test_expose_address_contract(value: str, accepted: bool) -> None:
    if accepted:
        assert (
            datastores.validate_bind_address(
                value,
                tailscale_ipv4={"100.64.0.7"},
            )
            == value
        )
    else:
        with pytest.raises(datastores.DatastoreExposureError):
            datastores.validate_bind_address(value, tailscale_ipv4={"100.64.0.7"})

    assert datastores.validate_published_host("hub.tailnet.ts.net") == "hub.tailnet.ts.net"
    for invalid_host in ("100.64.0.7", "::1", "*", "*.tailnet.ts.net"):
        with pytest.raises(datastores.DatastoreExposureError):
            datastores.validate_published_host(invalid_host)


@pytest.mark.parametrize(
    "operations",
    [
        ("expose", "expose"),
        ("expose", "services start"),
        ("expose", "services stop"),
        ("expose", "installer refresh"),
    ],
)
def test_managed_services_transitions_are_serialized(
    tmp_path: Path,
    operations: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    peak = 0
    guard = threading.Lock()
    first_acquired = threading.Event()
    second_blocked = threading.Event()
    original_try_acquire = managed_services_lock_module._try_acquire

    def observed_try_acquire(lock_file: Any) -> bool:
        acquired = original_try_acquire(lock_file)
        if threading.current_thread().name == "second" and not acquired:
            second_blocked.set()
        return acquired

    monkeypatch.setattr(managed_services_lock_module, "_try_acquire", observed_try_acquire)

    def transition(name: str, *, first: bool) -> None:
        nonlocal active, peak
        if not first:
            first_acquired.wait(timeout=2)
        with managed_services_lock(tmp_path, operation=name, timeout=2):
            with guard:
                active += 1
                peak = max(peak, active)
            if first:
                first_acquired.set()
                second_blocked.wait(timeout=2)
            with guard:
                active -= 1

    threads = [
        threading.Thread(
            target=transition,
            args=(operations[0],),
            kwargs={"first": True},
            name="first",
        ),
        threading.Thread(
            target=transition,
            args=(operations[1],),
            kwargs={"first": False},
            name="second",
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert second_blocked.is_set()
    assert peak == 1

    with managed_services_lock(tmp_path, operation="outer", timeout=2):
        with managed_services_lock(tmp_path, operation="reentrant", timeout=2):
            pass
