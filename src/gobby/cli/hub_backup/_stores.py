"""Per-store backup drivers for `gobby hub-backup`.

Each driver produces artifacts under a backup root and reports the facts the
manifest needs. Verification of those artifacts lives in `_verify`; nothing
here proves a backup is restorable, it only makes one.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 # fixed docker/pg_dump/redis-cli argv, never shell=True
import time
from collections.abc import Sequence
from pathlib import Path

import click
import httpx
import psycopg
from qdrant_client import QdrantClient

from gobby.cli.hub_backup._manifest import ArtifactRecord, SourceIdentity
from gobby.cli.installers.container_restart import FALKORDB_CONTAINER, POSTGRES_CONTAINER
from gobby.cli.installers.postgres import DEFAULT_POSTGRES_DB, DEFAULT_POSTGRES_USER
from gobby.cli.postgres_backup import (
    _docker_pg_dump_timeout_seconds,
    _dsn_db,
    _dsn_user,
    _process_output,
    _raise_for_subprocess_error,
    _sha256_file,
)
from gobby.storage.maintenance_epoch import MAINTENANCE_EPOCH_ENV

POSTGRES_DUMP_RELPATH = "postgres/gobby.dump"
GLOBALS_DUMP_RELPATH = "postgres/globals.sql"
QDRANT_SNAPSHOT_DIR = "qdrant"
FALKORDB_DUMP_RELPATH = "falkordb/dump.rdb"
VOLUME_ARCHIVE_DIR = "volumes"

HUB_VOLUMES: tuple[str, ...] = (
    "gobby_postgres_data",
    "gobby_pgaudit_log",
    "gobby_qdrant_data",
    "gobby_falkordb_data",
)

FALKORDB_BGSAVE_TIMEOUT_SECONDS = 300
FALKORDB_BGSAVE_POLL_SECONDS = 0.5

_CONNECT_TIMEOUT_SECONDS = 10
_ARCHIVE_LIST_TIMEOUT_SECONDS = 120
_REDIS_TIMEOUT_SECONDS = 60
_DOCKER_CP_TIMEOUT_SECONDS = 600
_VOLUME_ARCHIVE_TIMEOUT_SECONDS = 3600
_QDRANT_TIMEOUT_SECONDS = 120

_FALKORDB_RDB_PATH = "/var/lib/falkordb/data/dump.rdb"

_REDIS_LASTSAVE = "LASTSAVE"
_REDIS_BGSAVE = "BGSAVE"
_REDIS_INFO_PERSISTENCE = "INFO persistence"
_REDIS_GRAPH_LIST = "GRAPH.LIST"
_REDIS_DBSIZE = "DBSIZE"

_TABLE_LIST_SQL = "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
_ROLE_LIST_SQL = (
    "SELECT rolname, rolsuper, rolcanlogin FROM pg_roles "
    "WHERE rolname NOT LIKE 'pg\\_%' ORDER BY rolname"
)


# ---------------------------------------------------------------------------
# PostgreSQL source facts
# ---------------------------------------------------------------------------


def collect_postgres_identity(database_url: str) -> tuple[SourceIdentity, int]:
    """Return the source database identity and its schema-migration head."""
    try:
        with psycopg.connect(database_url, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn:
            system_row = conn.execute(
                "SELECT system_identifier FROM pg_control_system()"
            ).fetchone()
            name_row = conn.execute("SELECT current_database()").fetchone()
            oid_row = conn.execute(
                "SELECT oid FROM pg_database WHERE datname = current_database()"
            ).fetchone()
            head_row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except psycopg.Error as exc:
        raise click.ClickException(f"Unable to read PostgreSQL source identity: {exc}") from exc

    if system_row is None or name_row is None or oid_row is None:
        raise click.ClickException("PostgreSQL did not report a source identity")
    if head_row is None or head_row[0] is None:
        raise click.ClickException(
            "schema_migrations has no applied version; refusing to back up an unmigrated database"
        )

    identity = SourceIdentity(
        pg_system_identifier=str(system_row[0]),
        database_name=str(name_row[0]),
        database_oid=int(oid_row[0]),
    )
    return identity, int(head_row[0])


def collect_row_count_probes(database_url: str) -> dict[str, int]:
    """Return an exact row count for every table in the public schema."""
    probes: dict[str, int] = {}
    try:
        with psycopg.connect(database_url, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn:
            tables = [str(row[0]) for row in conn.execute(_TABLE_LIST_SQL).fetchall()]
            for table in tables:
                # The identifier comes from pg_tables and is quote-escaped, never user input.
                count_row = conn.execute(
                    f"SELECT count(*) FROM public.{_quote_identifier(table)}"  # nosec B608
                ).fetchone()
                probes[table] = int(count_row[0]) if count_row else 0
    except psycopg.Error as exc:
        raise click.ClickException(f"Unable to collect row-count probes: {exc}") from exc
    return probes


def collect_source_roles(database_url: str) -> list[dict[str, object]]:
    """Return the non-builtin roles the restore target must reproduce."""
    try:
        with psycopg.connect(database_url, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn:
            rows = conn.execute(_ROLE_LIST_SQL).fetchall()
    except psycopg.Error as exc:
        raise click.ClickException(f"Unable to read PostgreSQL roles: {exc}") from exc
    return [
        {
            "rolname": str(row[0]),
            "rolsuper": bool(row[1]),
            "rolcanlogin": bool(row[2]),
        }
        for row in rows
    ]


def dump_postgres(
    database_url: str,
    backup_root: Path,
) -> tuple[list[ArtifactRecord], dict[str, object]]:
    """Dump the database and cluster globals, then check the archive is readable.

    The dump deliberately keeps ownership and ACLs: roles are restored from the
    globals dump first, so a stripped dump would silently drop privileges.
    """
    user = _dsn_user(database_url) or DEFAULT_POSTGRES_USER
    database = _dsn_db(database_url) or DEFAULT_POSTGRES_DB
    postgres_version = _server_version(database_url)
    dump_timeout = _docker_pg_dump_timeout_seconds()

    dump_path = _prepare_artifact_path(backup_root, POSTGRES_DUMP_RELPATH)
    _capture_stdout(
        _postgres_client_command("pg_dump", "-U", user, "-d", database, "-Fc"),
        dump_path,
        action="Docker pg_dump",
        timeout=dump_timeout,
    )

    globals_path = _prepare_artifact_path(backup_root, GLOBALS_DUMP_RELPATH)
    _capture_stdout(
        _postgres_client_command("pg_dumpall", "-U", user, "--globals-only"),
        globals_path,
        action="Docker pg_dumpall --globals-only",
        timeout=dump_timeout,
    )

    _check_archive_readable(dump_path)

    artifacts = [
        _artifact_record("postgres-dump", backup_root, POSTGRES_DUMP_RELPATH),
        _artifact_record("postgres-globals", backup_root, GLOBALS_DUMP_RELPATH),
    ]
    details: dict[str, object] = {
        "postgres_version": postgres_version,
        "archive_list_checked": True,
    }
    return artifacts, details


def _postgres_client_command(client: str, *args: str) -> list[str]:
    command = ["docker", "exec"]
    if os.environ.get(MAINTENANCE_EPOCH_ENV):
        command.extend(["-e", "PGOPTIONS"])
    return [*command, POSTGRES_CONTAINER, client, *args]


def _server_version(database_url: str) -> str:
    try:
        with psycopg.connect(database_url, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn:
            row = conn.execute("SHOW server_version").fetchone()
    except psycopg.Error as exc:
        raise click.ClickException(f"Unable to read PostgreSQL server version: {exc}") from exc
    return str(row[0]) if row else "unknown"


def _check_archive_readable(dump_path: Path) -> None:
    if not dump_path.is_file():
        raise click.ClickException(f"PostgreSQL dump was not created: {dump_path}")
    command = ["docker", "exec", "-i", POSTGRES_CONTAINER, "pg_restore", "--list"]
    try:
        with dump_path.open("rb") as stdin:
            result = subprocess.run(  # nosec B603
                command,
                stdin=stdin,
                capture_output=True,
                timeout=_ARCHIVE_LIST_TIMEOUT_SECONDS,
            )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        raise click.ClickException(f"Docker pg_restore --list failed: {exc}") from exc
    _raise_for_subprocess_error(result, "Docker pg_restore --list")


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------


def snapshot_qdrant(
    url: str | None,
    api_key: str | None,
    backup_root: Path,
) -> tuple[list[ArtifactRecord], dict[str, object]]:
    """Snapshot every Qdrant collection and download each snapshot."""
    if url is None:
        raise click.ClickException(
            "Qdrant snapshot requires the Docker-managed Qdrant server; no server URL is "
            "configured for this hub."
        )

    client = QdrantClient(url=url, api_key=api_key, timeout=_QDRANT_TIMEOUT_SECONDS)
    headers = {"api-key": api_key} if api_key else {}
    artifacts: list[ArtifactRecord] = []
    collections: dict[str, dict[str, object]] = {}

    for name in sorted(item.name for item in client.get_collections().collections):
        points = int(client.count(name, exact=True).count)
        snapshot = client.create_snapshot(collection_name=name, wait=True)
        if snapshot is None or not snapshot.name:
            raise click.ClickException(f"Qdrant did not create a snapshot for collection {name}")

        relpath = f"{QDRANT_SNAPSHOT_DIR}/{name}.snapshot"
        destination = _prepare_artifact_path(backup_root, relpath)
        _download_qdrant_snapshot(
            url=url,
            collection=name,
            snapshot_name=snapshot.name,
            headers=headers,
            destination=destination,
        )
        client.delete_snapshot(collection_name=name, snapshot_name=snapshot.name, wait=True)

        artifacts.append(_artifact_record(f"qdrant-{name}", backup_root, relpath))
        collections[name] = {"points": points, "snapshot": relpath}

    return artifacts, {"collections": collections}


def _download_qdrant_snapshot(
    *,
    url: str,
    collection: str,
    snapshot_name: str,
    headers: dict[str, str],
    destination: Path,
) -> None:
    endpoint = f"{url.rstrip('/')}/collections/{collection}/snapshots/{snapshot_name}"
    try:
        with httpx.stream(
            "GET",
            endpoint,
            headers=headers,
            timeout=_QDRANT_TIMEOUT_SECONDS,
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"Qdrant snapshot download failed for collection {collection}: {exc}"
        ) from exc
    destination.chmod(0o600)


# ---------------------------------------------------------------------------
# FalkorDB
# ---------------------------------------------------------------------------


def dump_falkordb(backup_root: Path) -> tuple[list[ArtifactRecord], dict[str, object]]:
    """Force a fresh RDB save and copy it out of the FalkorDB container."""
    previous_save = _falkordb_lastsave()
    _redis_cli(_REDIS_BGSAVE)
    _await_falkordb_bgsave(previous_save)

    graphs = sorted(
        line.strip() for line in _redis_cli(_REDIS_GRAPH_LIST).splitlines() if line.strip()
    )
    dbsize = _parse_int(_redis_cli(_REDIS_DBSIZE), what="FalkorDB DBSIZE")

    destination = _prepare_artifact_path(backup_root, FALKORDB_DUMP_RELPATH)
    _copy_from_container(f"{FALKORDB_CONTAINER}:{_FALKORDB_RDB_PATH}", destination)

    artifacts = [_artifact_record("falkordb-rdb", backup_root, FALKORDB_DUMP_RELPATH)]
    return artifacts, {"graphs": graphs, "dbsize": dbsize}


def _await_falkordb_bgsave(previous_save: int) -> None:
    deadline = time.monotonic() + FALKORDB_BGSAVE_TIMEOUT_SECONDS
    while True:
        fields = _parse_redis_info(_redis_cli(_REDIS_INFO_PERSISTENCE))
        current_save = _falkordb_lastsave()
        if fields.get("rdb_bgsave_in_progress") == "0" and current_save > previous_save:
            status = fields.get("rdb_last_bgsave_status", "")
            if status != "ok":
                raise click.ClickException(
                    f"FalkorDB BGSAVE finished with status {status or 'unknown'}"
                )
            return
        if time.monotonic() >= deadline:
            raise click.ClickException(
                f"FalkorDB BGSAVE did not complete within {FALKORDB_BGSAVE_TIMEOUT_SECONDS} seconds"
            )
        time.sleep(FALKORDB_BGSAVE_POLL_SECONDS)


def _falkordb_lastsave() -> int:
    return _parse_int(_redis_cli(_REDIS_LASTSAVE), what="FalkorDB LASTSAVE")


def _redis_cli(request: str) -> str:
    """Run a fixed redis-cli request inside the FalkorDB container.

    The password stays inside the container: `sh -c` expands the container's
    own environment variable, so it never enters this process's argv.
    """
    command = [
        "docker",
        "exec",
        FALKORDB_CONTAINER,
        "sh",
        "-c",
        f'redis-cli -a "$GOBBY_FALKORDB_PASSWORD" --no-auth-warning {request}',
    ]
    action = f"FalkorDB {request}"
    try:
        result = subprocess.run(  # nosec B603
            command,
            capture_output=True,
            timeout=_REDIS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        raise click.ClickException(f"{action} failed: {exc}") from exc
    _raise_for_subprocess_error(result, action)
    return _process_output(result.stdout)


def _parse_redis_info(payload: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in payload.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or ":" not in entry:
            continue
        key, _, value = entry.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _copy_from_container(source: str, destination: Path) -> None:
    command = ["docker", "cp", source, str(destination)]
    try:
        result = subprocess.run(  # nosec B603
            command,
            capture_output=True,
            timeout=_DOCKER_CP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        raise click.ClickException(f"Docker cp of {source} failed: {exc}") from exc
    _raise_for_subprocess_error(result, f"Docker cp of {source}")
    if not destination.is_file():
        raise click.ClickException(f"Docker cp produced no file at {destination}")
    destination.chmod(0o600)


# ---------------------------------------------------------------------------
# Docker volumes
# ---------------------------------------------------------------------------


def tar_volumes(
    backup_root: Path,
    volumes: Sequence[str] = HUB_VOLUMES,
) -> tuple[list[ArtifactRecord], dict[str, object]]:
    """Archive each hub Docker volume. The caller must have stopped the services."""
    artifacts: list[ArtifactRecord] = []
    archived: list[str] = []
    for volume in volumes:
        relpath = f"{VOLUME_ARCHIVE_DIR}/{volume}.tar.gz"
        destination = _prepare_artifact_path(backup_root, relpath)
        command = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume}:/source:ro",
            "-v",
            f"{destination.parent}:/backup",
            "alpine",
            "tar",
            "czf",
            f"/backup/{destination.name}",
            "-C",
            "/source",
            ".",
        ]
        action = f"Docker volume archive for {volume}"
        try:
            result = subprocess.run(  # nosec B603
                command,
                capture_output=True,
                timeout=_VOLUME_ARCHIVE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
            raise click.ClickException(f"{action} failed: {exc}") from exc
        _raise_for_subprocess_error(result, action)
        if not destination.is_file():
            raise click.ClickException(f"{action} produced no archive at {destination}")
        destination.chmod(0o600)
        artifacts.append(_artifact_record(f"volume-{volume}", backup_root, relpath))
        archived.append(volume)
    return artifacts, {"volumes": archived}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _capture_stdout(command: list[str], destination: Path, *, action: str, timeout: int) -> None:
    try:
        with destination.open("wb") as output:
            result = subprocess.run(  # nosec B603
                command,
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        raise click.ClickException(f"{action} failed: {exc}") from exc
    _raise_for_subprocess_error(result, action)
    destination.chmod(0o600)


def _prepare_artifact_path(backup_root: Path, relpath: str) -> Path:
    """Create the artifact's parent directories as 0700 and return its path."""
    destination = backup_root / relpath
    destination.parent.mkdir(parents=True, exist_ok=True)
    directory = backup_root
    directory.chmod(0o700)
    for part in Path(relpath).parent.parts:
        directory = directory / part
        directory.chmod(0o700)
    return destination


def _artifact_record(name: str, backup_root: Path, relpath: str) -> ArtifactRecord:
    path = backup_root / relpath
    return ArtifactRecord(
        name=name,
        path=relpath,
        sha256=_sha256_file(path),
        size_bytes=path.stat().st_size,
    )


def _quote_identifier(identifier: str) -> str:
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def _parse_int(payload: str, *, what: str) -> int:
    try:
        return int(payload.strip())
    except ValueError as exc:
        raise click.ClickException(f"{what} returned a non-numeric reply: {payload!r}") from exc
