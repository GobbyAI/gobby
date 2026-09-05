"""Dedicated rehearsal admission and lifecycle must never target shared services."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import click
import psycopg
import pytest
from click.testing import CliRunner
from psycopg.conninfo import conninfo_to_dict

from gobby.cli import postgres_backup
from gobby.cli.hub_backup import cli, rehearsal
from gobby.cli.hub_backup.files_home import maintenance_claim
from gobby.runner_pid_file import claim_pid_file
from gobby.storage.maintenance_epoch import MaintenanceEpoch

hub_maintenance = import_module("gobby.cli.hub_maintenance")


@dataclass
class RehearsalHarness:
    path: Path
    data: dict[str, Any]
    containers: dict[str, Any]
    volumes: dict[str, Any]
    calls: list[list[str]]
    connect: MagicMock
    foreign_volume_user: bool = False

    @property
    def database_url(self) -> str:
        port = self.data["services"]["postgres"]["port"]
        return f"postgresql://gobby_test:isolated@127.0.0.1:{port}/gobby_test"

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data))
        self.path.chmod(0o600)

    def run(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command == ["sysctl", "-n", "kern.boottime"]:
            return subprocess.CompletedProcess(command, 0, stdout="fixture-boot\n", stderr="")
        self.calls.append(command)
        args = command[1:]
        if args[:2] == ["container", "inspect"]:
            output = json.dumps([self.containers[args[2]]])
        elif args[:2] == ["volume", "inspect"]:
            output = json.dumps([self.volumes[args[2]]])
        elif args[0] == "ps":
            volume = args[-1].split("=", 1)[1]
            output = next(
                service["container_id"]
                for service in self.data["services"].values()
                if service["volume"] == volume
            )
            if self.foreign_volume_user:
                output += "\n" + "f" * 64
        elif args[0] in {"stop", "start"}:
            for identity in args[1:]:
                self.containers[identity]["State"]["Running"] = args[0] == "start"
            output = ""
        elif args[0] == "exec":
            output = "archive listing"
        else:
            raise AssertionError(f"Unexpected Docker command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")


@pytest.fixture
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RehearsalHarness:
    home = tmp_path.resolve() / "rehearsal-home"
    home.mkdir(mode=0o700)
    path = home / "profile.json"
    data: dict[str, Any] = {
        "version": 1,
        "run_id": "ecb7b773-3c92-4ecb-8d88-ac557347a989",
        "owner_uid": os.getuid(),
        "gobby_home": str(home),
        "postgres_system_identifier": "987654321",
        "qdrant_url": "http://127.0.0.1:6339",
        "falkordb_url": "redis://127.0.0.1:16390",
        "services": {},
    }
    containers, volumes = {}, {}
    for digit, (role, inner, outer, mount) in enumerate(
        (
            ("postgres", 5432, 60894, "/var/lib/postgresql"),
            ("qdrant", 6333, 6339, "/qdrant/storage"),
            ("falkordb", 6379, 16390, "/var/lib/falkordb/data"),
        ),
        start=1,
    ):
        identity = str(digit) * 64
        name = f"gobby-rehearsal-e2e-{role}"
        volume = name + "-data"
        labels = {
            f"{rehearsal.LABEL_PREFIX}run_id": data["run_id"],
            f"{rehearsal.LABEL_PREFIX}owner_uid": str(os.getuid()),
            f"{rehearsal.LABEL_PREFIX}service": role,
        }
        data["services"][role] = {
            "name": name,
            "container_id": identity,
            "port": outer,
            "volume": volume,
        }
        containers[identity] = {
            "Id": identity,
            "Name": f"/{name}",
            "Config": {"Labels": dict(labels)},
            "State": {"Running": True},
            "HostConfig": {
                "NetworkMode": "bridge",
                "Privileged": False,
                "PortBindings": {f"{inner}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(outer)}]},
            },
            "Mounts": [{"Type": "volume", "Name": volume, "Destination": mount}],
        }
        volumes[volume] = {"Name": volume, "Driver": "local", "Options": {}, "Labels": dict(labels)}
    connect = MagicMock()
    connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (
        data["postgres_system_identifier"],
    )
    harness = RehearsalHarness(path, data, containers, volumes, [], connect)
    harness.save()
    monkeypatch.setenv(rehearsal.PROFILE_ENV, str(path))
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.delenv("PGHOSTADDR", raising=False)
    monkeypatch.delenv("PGSERVICE", raising=False)
    monkeypatch.setattr(rehearsal, "ensure_docker_allowed", MagicMock())
    monkeypatch.setattr(rehearsal, "discover_active_maintenance_epoch", lambda url: None)
    monkeypatch.setattr(subprocess, "run", harness.run)
    monkeypatch.setattr(psycopg, "connect", connect)
    return harness


def test_profile_routes_backup_to_owned_datastores(stack: RehearsalHarness) -> None:
    target = cli._hub_backup_target(stack.database_url)
    assert target.rehearsal is not None
    assert target.containers == tuple(str(n) * 64 for n in (1, 2, 3))
    assert target.falkordb_container == "3" * 64
    assert target.qdrant_port == 6339
    assert target.rehearsal.qdrant_url == stack.data["qdrant_url"]
    assert postgres_backup._managed_postgres_container(stack.database_url) == "1" * 64
    assert all(command[1] in {"container", "volume", "ps"} for command in stack.calls)


@pytest.mark.parametrize("port", [60891, 60892, 6333, 6379, 60990])
def test_profile_rejects_shared_endpoints_before_access(stack: RehearsalHarness, port: int) -> None:
    stack.data["services"]["postgres"]["port"] = port
    stack.save()
    with pytest.raises(click.ClickException, match="production or shared"):
        rehearsal.load_rehearsal_profile(stack.database_url)
    assert stack.calls == []
    stack.connect.assert_not_called()


@pytest.mark.parametrize("target", ["profile", "home", "environment"])
def test_profile_rejects_symlink_ancestors_before_access(
    stack: RehearsalHarness, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    home = stack.path.parent
    alias = home.parent / "ancestor-alias"
    alias.symlink_to(home.parent, target_is_directory=True)
    aliased_home = alias / home.name
    if target == "profile":
        monkeypatch.setenv(rehearsal.PROFILE_ENV, str(aliased_home / stack.path.name))
    elif target == "home":
        stack.data["gobby_home"] = str(aliased_home)
        stack.save()
    else:
        monkeypatch.setenv("GOBBY_HOME", str(aliased_home))
    with pytest.raises(click.ClickException, match="symlink path component"):
        rehearsal.load_rehearsal_profile(stack.database_url)
    assert stack.calls == []
    stack.connect.assert_not_called()


@pytest.mark.parametrize("failure", ["unprotected", "public", "symlink", "owner", "remote"])
def test_profile_rejects_unsafe_configuration(
    stack: RehearsalHarness, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "unprotected":
        monkeypatch.delenv("GOBBY_TEST_PROTECT")
    elif failure == "public":
        stack.path.chmod(0o644)
    elif failure == "symlink":
        actual = stack.path.with_suffix(".actual")
        stack.path.rename(actual)
        stack.path.symlink_to(actual)
    elif failure == "owner":
        stack.data["owner_uid"] += 1
        stack.save()
    else:
        stack.data["qdrant_url"] = "http://remote.example:6339"
        stack.save()
    with pytest.raises(click.ClickException, match="Rehearsal|rehearsal"):
        rehearsal.load_rehearsal_profile(stack.database_url)
    assert stack.calls == []
    stack.connect.assert_not_called()


@pytest.mark.parametrize("failure", ["id", "labels", "mount", "shared", "network", "malformed"])
def test_profile_rejects_unowned_docker_state(stack: RehearsalHarness, failure: str) -> None:
    record = stack.containers["1" * 64]
    if failure == "id":
        record["Id"] = "f" * 64
    elif failure == "labels":
        record["Config"]["Labels"][f"{rehearsal.LABEL_PREFIX}run_id"] = "another-run"
    elif failure == "mount":
        record["Mounts"].append({"Type": "bind", "Source": "/unrelated"})
    elif failure == "shared":
        stack.foreign_volume_user = True
    elif failure == "network":
        record["HostConfig"]["NetworkMode"] = "host"
    else:
        record["Config"] = None
    with pytest.raises(click.ClickException, match="Rehearsal|rehearsal"):
        rehearsal.load_rehearsal_profile(stack.database_url)
    assert all(command[1] in {"container", "volume", "ps"} for command in stack.calls)
    stack.connect.assert_not_called()


def test_profile_rejects_falkordb_mount_outside_the_image_storage_directory(
    stack: RehearsalHarness,
) -> None:
    identity = stack.data["services"]["falkordb"]["container_id"]
    stack.containers[identity]["Mounts"][0]["Destination"] = "/data"
    with pytest.raises(click.ClickException, match="Rehearsal data volume does not match"):
        rehearsal.load_rehearsal_profile(stack.database_url)
    assert all(command[1] in {"container", "volume", "ps"} for command in stack.calls)
    stack.connect.assert_not_called()


def test_profile_binds_discovered_epoch_without_changing_environment(
    stack: RehearsalHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    epoch = MaintenanceEpoch(
        id=UUID("a260be66-d16e-4a27-bc5b-d1d1b8d2c617"),
        campaign="schema-apply",
        opened_at=datetime(2026, 9, 5, tzinfo=UTC),
        opened_by="hub-maintenance:schema-apply",
        scope_note="isolated test",
        released_at=None,
        released_by_command=None,
    )
    discover = MagicMock(return_value=epoch)
    monkeypatch.setattr(rehearsal, "discover_active_maintenance_epoch", discover)
    before = dict(os.environ)
    assert rehearsal.load_rehearsal_profile(stack.database_url) is not None
    discover.assert_called_once_with(stack.database_url)
    target = conninfo_to_dict(stack.connect.call_args.args[0])
    assert target.pop("options") == f"-c gobby.maintenance_epoch={epoch.id}"
    assert target == conninfo_to_dict(stack.database_url)
    assert dict(os.environ) == before


def test_profile_identity_without_epoch_keeps_original_connection_target(
    stack: RehearsalHarness,
) -> None:
    assert rehearsal.load_rehearsal_profile(stack.database_url) is not None
    stack.connect.assert_called_once_with(stack.database_url, connect_timeout=5, autocommit=True)


@pytest.mark.parametrize("stage", ["discovery", "identity"])
def test_profile_refuses_unavailable_epoch_or_identity(
    stack: RehearsalHarness, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    error = psycopg.OperationalError("isolated connection unavailable")
    if stage == "discovery":
        monkeypatch.setattr(
            rehearsal, "discover_active_maintenance_epoch", MagicMock(side_effect=error)
        )
    else:
        stack.connect.side_effect = error
    with pytest.raises(
        click.ClickException, match="Could not verify rehearsal PostgreSQL identity"
    ):
        rehearsal.load_rehearsal_profile(stack.database_url)
    assert all(command[1] in {"container", "volume", "ps"} for command in stack.calls)
    if stage == "discovery":
        stack.connect.assert_not_called()


def test_profile_rejects_wrong_database_and_source_identity(stack: RehearsalHarness) -> None:
    with pytest.raises(click.ClickException, match="exact loopback gobby_test"):
        rehearsal.load_rehearsal_profile(stack.database_url.replace("/gobby_test", "/gobby"))
    stack.connect.assert_not_called()
    stack.connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (
        "another-cluster",
    )
    with pytest.raises(click.ClickException, match="source identity changed"):
        rehearsal.load_rehearsal_profile(stack.database_url)
    assert all(command[1] in {"container", "volume", "ps"} for command in stack.calls)


@pytest.mark.parametrize("role", ["postgres", "qdrant", "falkordb"])
def test_profile_rejects_an_endpoint_on_a_different_loopback_address(
    stack: RehearsalHarness, role: str
) -> None:
    service = stack.data["services"][role]
    bindings = stack.containers[service["container_id"]]["HostConfig"]["PortBindings"]
    next(iter(bindings.values()))[0]["HostIp"] = "::1"
    with pytest.raises(click.ClickException, match="endpoint binding"):
        rehearsal.load_rehearsal_profile(stack.database_url)
    stack.connect.assert_not_called()


def test_cold_archive_restarts_only_owned_containers_after_failure(
    stack: RehearsalHarness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = rehearsal.load_rehearsal_profile(stack.database_url)
    assert profile is not None
    tar = MagicMock(side_effect=RuntimeError("archive interrupted"))
    monkeypatch.setattr(cli, "tar_volumes", tar)
    global_stop = MagicMock(side_effect=AssertionError("global stop called"))
    global_start = MagicMock(side_effect=AssertionError("global start called"))
    monkeypatch.setattr(cli, "_services_stop", global_stop)
    monkeypatch.setattr(cli, "_services_start", global_start)
    with pytest.raises(RuntimeError, match="archive interrupted"):
        cli._archive_volumes(
            stack.path.parent, tmp_path, volumes=profile.volumes, rehearsal=profile
        )
    assert [c for c in stack.calls if c[1] in {"stop", "start"}] == [
        ["docker", "stop", *profile.containers],
        ["docker", "start", *profile.containers],
    ]
    tar.assert_called_once_with(tmp_path, profile.volumes)
    global_stop.assert_not_called()
    global_start.assert_not_called()


def test_partial_stop_failure_still_restarts_all_owned_containers(
    stack: RehearsalHarness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = rehearsal.load_rehearsal_profile(stack.database_url)
    assert profile is not None

    def partial_stop(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[1] == "stop":
            stack.calls.append(command)
            stack.containers[profile.containers[0]]["State"]["Running"] = False
            raise subprocess.CalledProcessError(1, command)
        return stack.run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", partial_stop)
    tar = MagicMock()
    monkeypatch.setattr(cli, "tar_volumes", tar)
    with pytest.raises(click.ClickException, match="Docker operation failed: stop"):
        cli._archive_volumes(
            stack.path.parent, tmp_path, volumes=profile.volumes, rehearsal=profile
        )
    assert ["docker", "start", *profile.containers] in stack.calls
    assert all(record["State"]["Running"] for record in stack.containers.values())
    tar.assert_not_called()


def test_archive_readability_probe_uses_owned_postgres(
    stack: RehearsalHarness, tmp_path: Path
) -> None:
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"fixture archive")
    postgres_backup._verify_dump_with_pg_restore(dump_path=archive)
    assert stack.calls[-1] == ["docker", "exec", "-i", "1" * 64, "pg_restore", "--list"]


def test_rehearsal_maintenance_never_controls_installed_daemon(
    stack: RehearsalHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.cli.installers import service

    stop = MagicMock(side_effect=AssertionError("global daemon stop called"))
    start = MagicMock(side_effect=AssertionError("global daemon start called"))
    monkeypatch.setattr(hub_maintenance, "stop_daemon", stop)
    monkeypatch.setattr(service, "get_service_status", start)
    hub_maintenance._stop_daemon_before_fence(stack.database_url)
    cli._start_daemon()
    stop.assert_not_called()
    start.assert_not_called()
    assert all(command[1] in {"container", "volume", "ps"} for command in stack.calls)
    pid_file = stack.path.parent / "gobby.pid"
    pid_file.write_text(str(os.getpid()))
    with pytest.raises(click.ClickException, match="daemonless"):
        hub_maintenance._stop_daemon_before_fence(stack.database_url)
    stop.assert_not_called()
    assert pid_file.read_text() == str(os.getpid())


def test_profile_accepts_its_held_maintenance_claim_and_rejects_release(
    stack: RehearsalHarness,
) -> None:
    home = stack.path.parent
    with maintenance_claim(home) as claim:
        profile = rehearsal.load_rehearsal_profile(stack.database_url)
        assert profile is not None
        assert profile.gobby_home == str(home)
        assert claim.role == "maintenance"
        assert (home / "gobby.pid").read_text() == str(os.getpid())
    with pytest.raises(click.ClickException, match="daemonless"):
        rehearsal.load_rehearsal_profile(stack.database_url)


@pytest.mark.parametrize("failure", ["daemon", "foreign_home", "foreign_pid"])
def test_profile_rejects_other_live_singleton_claims(stack: RehearsalHarness, failure: str) -> None:
    home = stack.path.parent
    claim_home = home
    if failure == "foreign_home":
        claim_home = home.parent / "another-home"
        claim_home.mkdir(mode=0o700)
        (home / "gobby.pid").write_text(str(os.getpid()))
    claim = claim_pid_file(
        claim_home / "gobby.pid", role="daemon" if failure == "daemon" else "maintenance"
    )
    assert claim is not None
    try:
        if failure == "foreign_pid":
            (home / "gobby.pid").write_text(str(os.getppid()))
        with pytest.raises(click.ClickException, match="daemonless"):
            rehearsal.load_rehearsal_profile(stack.database_url)
        assert stack.calls == []
        stack.connect.assert_not_called()
    finally:
        claim.release()


def test_absent_profile_keeps_existing_managed_targets(
    stack: RehearsalHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(rehearsal.PROFILE_ENV)
    assert (
        postgres_backup._managed_postgres_container(
            "postgresql://gobby:gobby@127.0.0.1:60891/gobby"
        )
        == "gobby-postgres"
    )
    assert (
        postgres_backup._managed_postgres_container(
            "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
        )
        == "gobby-postgres-test-1"
    )
    assert stack.calls == []


def test_rehearsal_rejects_copied_log_path_before_backup_side_effects(
    stack: RehearsalHarness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "backup"
    stop = MagicMock()
    monkeypatch.setattr(cli, "stop_daemon", stop)
    monkeypatch.setattr(cli, "_preflight", MagicMock())
    monkeypatch.setattr(cli, "_resolve_database_url", lambda home: stack.database_url)
    monkeypatch.setattr(cli, "_configured_files_home", lambda home: home / "files")
    monkeypatch.setattr(cli, "_backup_logs_dir", lambda *args, **kwargs: tmp_path / "live-logs")
    result = CliRunner().invoke(cli.hub_backup, ["--output", str(output), "--json"])
    assert result.exit_code == 1
    assert "logs_dir must remain inside its private home" in result.output
    assert not output.exists()
    stop.assert_not_called()
