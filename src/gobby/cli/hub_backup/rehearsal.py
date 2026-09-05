"""Explicit Docker ownership for daemonless, isolated hub maintenance rehearsals."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from pwd import getpwuid
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

import click
import psutil
import psycopg
from psycopg.conninfo import conninfo_to_dict
from pydantic import BaseModel, ConfigDict, ValidationError

from gobby.cli.hub_backup._integrity import refuse_symlink_traversal
from gobby.cli.installers.docker_guard import ensure_docker_allowed
from gobby.paths import get_gobby_home
from gobby.runner_pid_file import held_singleton_claim
from gobby.storage.maintenance_epoch import (
    bind_maintenance_epoch,
    discover_active_maintenance_epoch,
)
from gobby.utils.env import is_test_protect_enabled

PROFILE_ENV = "GOBBY_HUB_REHEARSAL_PROFILE"
LABEL_PREFIX = "ai.gobby.rehearsal."
_ROLES = {"postgres": 5432, "qdrant": 6333, "falkordb": 6379}
_MOUNTS = {"postgres": "/var/lib/postgresql", "qdrant": "/qdrant/storage", "falkordb": "/data"}
_SHARED_PORTS = {5432, 6333, 6334, 6379, 16379, *range(60887, 60893), *range(60990, 61000)}
_LOOPBACK = {"127.0.0.1", "::1"}


class RehearsalService(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    container_id: str
    port: int
    volume: str


class RehearsalProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    run_id: UUID
    owner_uid: int
    gobby_home: str
    postgres_system_identifier: str
    postgres_host: Literal["127.0.0.1", "::1"] = "127.0.0.1"
    qdrant_url: str
    falkordb_url: str
    services: dict[str, RehearsalService]

    @property
    def containers(self) -> tuple[str, ...]:
        return tuple(self.services[role].container_id for role in _ROLES)

    @property
    def volumes(self) -> tuple[str, ...]:
        return tuple(self.services[role].volume for role in _ROLES)

    def require_daemonless(self) -> None:
        pid_file = Path(self.gobby_home) / "gobby.pid"
        if pid_file.is_symlink():
            raise click.ClickException("Rehearsal PID file must not be a symlink")
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
            except (OSError, ValueError) as exc:
                raise click.ClickException("Malformed rehearsal PID file") from exc
            if psutil.pid_exists(pid):
                claim = held_singleton_claim()
                if (
                    pid != os.getpid()
                    or claim is None
                    or claim.role != "maintenance"
                    or claim.lock_path != pid_file.with_name("gobby.pid.lock")
                ):
                    raise click.ClickException("Rehearsal requires a daemonless private home")

    def require_private_path(self, path: Path | None, label: str) -> None:
        if path is None or not path.resolve().is_relative_to(Path(self.gobby_home).resolve()):
            raise click.ClickException(f"Rehearsal {label} must remain inside its private home")


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    ensure_docker_allowed("isolated hub rehearsal", runner=subprocess.run)
    try:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise click.ClickException(f"Rehearsal Docker operation failed: {args[0]}") from exc


def _inspect(kind: str, identity: str) -> dict[str, Any]:
    try:
        payload = json.loads(_docker(kind, "inspect", identity).stdout)
    except json.JSONDecodeError as exc:
        raise click.ClickException("Malformed rehearsal Docker inspection") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise click.ClickException("Unexpected rehearsal Docker inspection")
    return payload[0]


def _labels(profile: RehearsalProfile, role: str, labels: object) -> None:
    expected = {
        f"{LABEL_PREFIX}run_id": str(profile.run_id),
        f"{LABEL_PREFIX}owner_uid": str(profile.owner_uid),
        f"{LABEL_PREFIX}service": role,
    }
    if not isinstance(labels, dict) or any(labels.get(k) != v for k, v in expected.items()):
        raise click.ClickException("Rehearsal Docker ownership labels do not match")


def _object(record: dict[str, Any], key: str) -> dict[str, Any]:
    value = record.get(key)
    if not isinstance(value, dict):
        raise click.ClickException("Malformed rehearsal Docker inspection")
    return value


def _validate_docker(profile: RehearsalProfile, *, require_running: bool) -> None:
    for role, service in profile.services.items():
        record = _inspect("container", service.container_id)
        if record.get("Id") != service.container_id or record.get("Name") != f"/{service.name}":
            raise click.ClickException("Rehearsal container identity changed")
        _labels(profile, role, _object(record, "Config").get("Labels"))
        host_config = _object(record, "HostConfig")
        network = str(host_config.get("NetworkMode", ""))
        if network == "host" or network.startswith("container:") or host_config.get("Privileged"):
            raise click.ClickException("Rehearsal containers require isolated Docker networking")
        if require_running and _object(record, "State").get("Running") is not True:
            raise click.ClickException("Rehearsal container is not running")
        bindings = _object(host_config, "PortBindings")
        published = {key: value for key, value in bindings.items() if value}
        expected_port = f"{_ROLES[role]}/tcp"
        expected_host = (
            profile.postgres_host
            if role == "postgres"
            else urlparse(profile.qdrant_url if role == "qdrant" else profile.falkordb_url).hostname
        )
        if (
            set(published) != {expected_port}
            or not isinstance(published[expected_port], list)
            or not all(
                isinstance(item, dict)
                and item.get("HostIp") == expected_host
                and item.get("HostPort") == str(service.port)
                for item in published.get(expected_port, [])
            )
        ):
            raise click.ClickException("Rehearsal Docker endpoint binding does not match")
        mounts = record.get("Mounts", [])
        if (
            not isinstance(mounts, list)
            or len(mounts) != 1
            or not isinstance(mounts[0], dict)
            or mounts[0].get("Type") != "volume"
        ):
            raise click.ClickException("Rehearsal requires one dedicated named data volume")
        if mounts[0].get("Name") != service.volume or mounts[0].get("Destination") != _MOUNTS[role]:
            raise click.ClickException("Rehearsal data volume does not match")
        volume = _inspect("volume", service.volume)
        if volume.get("Name") != service.volume or volume.get("Driver") != "local":
            raise click.ClickException("Rehearsal volume identity does not match")
        if volume.get("Options"):
            raise click.ClickException("Rehearsal volumes may not redirect to external storage")
        _labels(profile, role, volume.get("Labels"))
        users = _docker("ps", "-a", "-q", "--no-trunc", "--filter", f"volume={service.volume}")
        if set(users.stdout.split()) != {service.container_id}:
            raise click.ClickException("Rehearsal volume is shared with another container")


def _endpoint(url: str, scheme: str, port: int) -> None:
    try:
        parsed = urlparse(url)
        valid = (
            parsed.scheme == scheme
            and parsed.hostname in _LOOPBACK
            and parsed.port == port
            and not parsed.username
            and not parsed.password
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise click.ClickException("Rehearsal datastore endpoint does not match its owned service")


def _database_target(profile: RehearsalProfile, database_url: str) -> None:
    try:
        info = conninfo_to_dict(database_url)
    except psycopg.ProgrammingError as exc:
        raise click.ClickException("Malformed rehearsal database URL") from exc
    if (
        info.get("host") != profile.postgres_host
        or info.get("port") != str(profile.services["postgres"].port)
        or info.get("user") != "gobby_test"
        or info.get("dbname") != "gobby_test"
        or info.get("hostaddr")
        or info.get("service")
        or os.environ.get("PGHOSTADDR")
        or os.environ.get("PGSERVICE")
    ):
        raise click.ClickException("Rehearsal requires its exact loopback gobby_test database")
    try:
        epoch = discover_active_maintenance_epoch(database_url)
        identity_url = bind_maintenance_epoch(database_url, epoch.id) if epoch else database_url
        with psycopg.connect(identity_url, connect_timeout=5, autocommit=True) as connection:
            row = connection.execute("SELECT system_identifier FROM pg_control_system()").fetchone()
    except psycopg.Error as exc:
        raise click.ClickException("Could not verify rehearsal PostgreSQL identity") from exc
    if row is None or str(row[0]) != profile.postgres_system_identifier:
        raise click.ClickException("Rehearsal PostgreSQL source identity changed")


def load_rehearsal_profile(
    database_url: str | None = None, *, require_running: bool = True
) -> RehearsalProfile | None:
    """Validate the explicit profile; absence retains ordinary managed targets."""
    configured = os.environ.get(PROFILE_ENV)
    if not configured:
        return None
    if not is_test_protect_enabled():
        raise click.ClickException("Rehearsal profiles require GOBBY_TEST_PROTECT=1")
    path = Path(configured)
    if not path.is_absolute():
        raise click.ClickException("Rehearsal profile path must be absolute")
    refuse_symlink_traversal(path, label="Rehearsal profile")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.getuid()
            ):
                raise click.ClickException("Rehearsal profile must be owner-private mode0600")
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise click.ClickException("Rehearsal profile is too large")
        profile = RehearsalProfile.model_validate_json(raw)
    except (OSError, ValidationError) as exc:
        raise click.ClickException("Cannot read a valid owner-private rehearsal profile") from exc
    home = Path(profile.gobby_home)
    refuse_symlink_traversal(home, label="Rehearsal home")
    refuse_symlink_traversal(get_gobby_home(), label="GOBBY_HOME")
    if (
        profile.owner_uid != os.getuid()
        or not home.is_absolute()
        or home.is_symlink()
        or home.resolve() != get_gobby_home().resolve()
        or home.resolve() != path.parent.resolve()
        or home.resolve() == (Path(getpwuid(os.getuid()).pw_dir) / ".gobby").resolve()
        or home.stat().st_uid != os.getuid()
        or stat.S_IMODE(home.stat().st_mode) != 0o700
    ):
        raise click.ClickException("Rehearsal requires its own owner-private mode0700 GOBBY_HOME")
    if set(profile.services) != set(_ROLES):
        raise click.ClickException("Rehearsal requires exactly PostgreSQL, Qdrant and FalkorDB")
    if len({s.port for s in profile.services.values()}) != 3:
        raise click.ClickException("Rehearsal datastore ports must be distinct")
    for service in profile.services.values():
        if not 1024 <= service.port <= 65535 or service.port in _SHARED_PORTS:
            raise click.ClickException("Rehearsal cannot use a production or shared-service port")
        if not all(
            re.fullmatch(r"gobby-rehearsal-[a-zA-Z0-9_.-]+", name)
            for name in (service.name, service.volume)
        ) or not re.fullmatch(r"[a-f0-9]{64}", service.container_id):
            raise click.ClickException("Rehearsal requires dedicated names and full Docker IDs")
    if len(set(profile.containers)) != 3 or len(set(profile.volumes)) != 3:
        raise click.ClickException("Rehearsal containers and volumes must be distinct")
    _endpoint(profile.qdrant_url, "http", profile.services["qdrant"].port)
    _endpoint(profile.falkordb_url, "redis", profile.services["falkordb"].port)
    profile.require_daemonless()
    _validate_docker(profile, require_running=require_running)
    if database_url is not None:
        _database_target(profile, database_url)
    return profile


def control_rehearsal_services(profile: RehearsalProfile, action: Literal["stop", "start"]) -> None:
    """Recheck ownership immediately before controlling only the captured IDs."""
    current = load_rehearsal_profile(require_running=action == "stop")
    if current != profile:
        raise click.ClickException("Rehearsal profile changed during backup")
    _docker(action, *profile.containers)
