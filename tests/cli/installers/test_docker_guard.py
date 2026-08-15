"""Fail-closed contracts for real Docker execution under GOBBY_TEST_PROTECT."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from gobby.cli import _daemon_services
from gobby.cli.hub_backup import _stores as hub_stores
from gobby.cli.hub_backup import _verify as hub_verify
from gobby.cli.hub_backup import cli as hub_cli
from gobby.cli.installers import falkor
from gobby.cli.installers import postgres as postgres_installer
from gobby.cli.installers.compose_env import MANAGED_SERVICE_PROFILES, ComposeRuntime
from gobby.cli.installers.docker_guard import DockerTestProtectError, ensure_docker_allowed
from gobby.cli.pack import _import_docker_volume
from gobby.cli.postgres_backup import _run_pg_dump
from gobby.storage.maintenance_epoch import MAINTENANCE_EPOCH_ENV

pytestmark = pytest.mark.unit


def _protect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.delenv("GOBBY_TEST_ALLOW_DOCKER", raising=False)


def _write_compose(home: Path) -> Path:
    services = home / "services"
    services.mkdir(parents=True, exist_ok=True)
    compose_file = services / "docker-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    return compose_file


def test_real_runner_fails_closed_under_test_protect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _protect(monkeypatch)

    with pytest.raises(DockerTestProtectError, match="blocks real Docker execution"):
        ensure_docker_allowed("unit-test", runner=subprocess.run)


def test_stubbed_runner_passes_under_test_protect(monkeypatch: pytest.MonkeyPatch) -> None:
    _protect(monkeypatch)

    raised = False
    try:
        ensure_docker_allowed("unit-test", runner=lambda *args, **kwargs: None)
    except DockerTestProtectError:
        raised = True
    assert not raised, "a stubbed runner must pass the guard"


def test_real_runner_passes_without_test_protect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)

    raised = False
    try:
        ensure_docker_allowed("unit-test", runner=subprocess.run)
    except DockerTestProtectError:
        raised = True
    assert not raised, "the guard must be inert outside GOBBY_TEST_PROTECT"


def test_explicit_opt_in_allows_real_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.setenv("GOBBY_TEST_ALLOW_DOCKER", "1")

    raised = False
    try:
        ensure_docker_allowed("unit-test", runner=subprocess.run)
    except DockerTestProtectError:
        raised = True
    assert not raised, "GOBBY_TEST_ALLOW_DOCKER=1 must allow real execution"


def test_falkordb_uninstall_fails_closed_before_compose_down(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _protect(monkeypatch)
    _write_compose(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        falkor,
        "resolve_compose_runtime",
        lambda home, profiles: ComposeRuntime(environment={}, profiles=profiles),
    )

    with pytest.raises(DockerTestProtectError):
        falkor.uninstall_falkordb(gobby_home=tmp_path)


def test_postgres_install_fails_closed_before_compose_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _protect(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(postgres_installer, "_sync_postgres_pgsearch_assets", lambda **_kw: None)
    runtime = ComposeRuntime(environment={}, profiles=("postgres",))
    monkeypatch.setattr(
        postgres_installer,
        "_resolve_postgres_install_database_url",
        lambda **_kw: ("postgresql://gobby:pw@127.0.0.1:60891/gobby", runtime),
    )

    with pytest.raises(DockerTestProtectError):
        postgres_installer._install_docker(gobby_home=tmp_path, port=60891)


def test_managed_services_compose_up_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _protect(monkeypatch)
    compose_file = _write_compose(tmp_path)
    runtime = ComposeRuntime(environment={}, profiles=("postgres",))

    with pytest.raises(DockerTestProtectError):
        _daemon_services._run_compose_up(compose_file, compose_file.parent, runtime)


def test_managed_services_compose_down_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _protect(monkeypatch)
    _write_compose(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")

    def _resolve(
        gobby_home: Path,
        *,
        database_url: str | None = None,
        profiles: tuple[str, ...] = MANAGED_SERVICE_PROFILES,
        overrides: dict[str, str] | None = None,
    ) -> ComposeRuntime:
        return ComposeRuntime(environment={}, profiles=profiles)

    with pytest.raises(DockerTestProtectError):
        _daemon_services._stop_managed_services_locked(tmp_path, resolve_runtime=_resolve)


def test_postgres_backup_fails_closed_before_dump(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _protect(monkeypatch)
    dump_path = tmp_path / "postgres.dump"

    with pytest.raises(DockerTestProtectError, match="PostgreSQL backup dump"):
        _run_pg_dump(
            database_url="postgresql://gobby:secret@localhost:60891/gobby",
            dump_path=dump_path,
        )

    assert not dump_path.exists()


def test_pack_volume_import_fails_closed_before_volume_create(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _protect(monkeypatch)

    with pytest.raises(DockerTestProtectError, match="pack volume import"):
        _import_docker_volume("gobby_postgres_data", tmp_path / "volume.tar.gz")


def test_hub_backup_store_fails_closed_before_container_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _protect(monkeypatch)

    with pytest.raises(DockerTestProtectError, match="hub backup FalkorDB command"):
        hub_stores._redis_cli("gobby-falkordb", "PING")


def test_hub_backup_verification_fails_closed_before_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _protect(monkeypatch)

    with pytest.raises(DockerTestProtectError, match="hub backup restore verification"):
        hub_verify._docker("run", "--rm", "alpine", timeout=1)


def test_hub_backup_container_inspection_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _protect(monkeypatch)

    with pytest.raises(DockerTestProtectError, match="hub backup container inspection"):
        hub_cli._container_running("gobby-postgres")


def test_hub_backup_epoch_compose_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _protect(monkeypatch)
    epoch = "test-maintenance-epoch"
    monkeypatch.setenv(MAINTENANCE_EPOCH_ENV, epoch)
    runtime = ComposeRuntime(
        environment={"PGOPTIONS": f"-c gobby.maintenance_epoch={epoch}"},
        profiles=(),
    )

    with pytest.raises(DockerTestProtectError, match="hub backup epoch compose up"):
        hub_cli._run_epoch_compose_up(tmp_path / "docker-compose.yml", tmp_path, runtime)
