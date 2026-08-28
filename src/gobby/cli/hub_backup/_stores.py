"""Per-store backup drivers for `gobby hub-backup`.

Each driver produces artifacts under a backup root and reports the facts the
manifest needs. Verification of those artifacts lives in `_verify`; nothing
here proves a backup is restorable, it only makes one.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess  # nosec B404 # fixed docker/pg_dump/redis-cli argv, never shell=True
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

import click
import httpx
import psycopg
from qdrant_client import QdrantClient

from gobby.cli.hub_backup._content import qdrant_collection_digest, tar_stream_inventory
from gobby.cli.hub_backup._integrity import (
    file_digest,
    open_exclusive_binary,
    open_regular_binary,
    refuse_symlink_traversal,
    require_regular_file,
)
from gobby.cli.hub_backup._manifest import ArtifactRecord, SourceIdentity
from gobby.cli.installers.container_restart import FALKORDB_CONTAINER, POSTGRES_CONTAINER
from gobby.cli.installers.docker_guard import ensure_docker_allowed
from gobby.cli.installers.postgres import DEFAULT_POSTGRES_DB, DEFAULT_POSTGRES_USER
from gobby.cli.postgres_backup import (
    _docker_pg_dump_timeout_seconds,
    _dsn_db,
    _dsn_user,
    _managed_postgres_container,
    _process_output,
    _raise_for_subprocess_error,
)
from gobby.config.logging import RULE_ALLOW_AUDIT_LOG_FILENAME
from gobby.storage.maintenance_epoch import MAINTENANCE_EPOCH_ENV
from gobby.storage.managed_credential_types import auth_schema_for

POSTGRES_DUMP_RELPATH = "postgres/gobby.dump"
GLOBALS_DUMP_RELPATH = "postgres/globals.sql"
QDRANT_SNAPSHOT_DIR = "qdrant"
FALKORDB_DUMP_RELPATH = "falkordb/dump.rdb"
VOLUME_ARCHIVE_DIR = "volumes"
ALLOW_AUDIT_ARCHIVE_DIR = "logs"

HUB_VOLUMES: tuple[str, ...] = (
    "gobby_postgres_data",
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
# Managed principals -- scoped agent logins, per-session interactive principals,
# and maintenance logins -- are drained before the globals dump and re-minted on
# demand, so the restore target never has to reproduce them. This is the same
# namespace the schema reapers match (migrations 389/393/399).
_MANAGED_PRINCIPAL_PATTERN = (
    r"^(gobby_agent_[0-9a-f]{32}"
    r"|gobby_ix_([0-9a-f]{16}|[A-Za-z0-9]{1,8}_[0-9a-f]{8}_[0-9a-f]{8})"
    r"|gobby_mnt_[0-9a-f]{32})_[1-9][0-9]*$"
)
MANAGED_PRINCIPAL_RE = re.compile(_MANAGED_PRINCIPAL_PATTERN)
_ROLE_LIST_SQL = (
    "SELECT rolname, rolsuper, rolcanlogin FROM pg_roles "
    "WHERE rolname NOT LIKE 'pg\\_%' "
    f"AND rolname !~ '{_MANAGED_PRINCIPAL_PATTERN}' ORDER BY rolname"
)
_EPHEMERAL_ROLE_SQL = (
    "SELECT rolname, rolsuper, rolcanlogin FROM pg_roles "
    "WHERE rolcanlogin AND rolname ~ '^gobby_agent_[0-9a-f]{32}_[1-9][0-9]*$' "
    "ORDER BY rolname"
)
_ROLE_CREATE_LINE_RE = re.compile(rb"^CREATE ROLE .+;$", re.MULTILINE)
_ROLE_GRANTOR_RE = re.compile(
    rb" GRANTED BY (?:\"(?:[^\"]|\"\")*\"|[A-Za-z_][A-Za-z0-9_$]*);$",
    re.MULTILINE,
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


def collect_schema_object_counts(database_url: str) -> dict[str, int]:
    """Count public-schema object kinds for source-equivalent restore checks."""
    query = """
        SELECT CASE c.relkind
                   WHEN 'r' THEN 'table'
                   WHEN 'p' THEN 'partitioned_table'
                   WHEN 'i' THEN 'index'
                   WHEN 'I' THEN 'partitioned_index'
                   WHEN 'S' THEN 'sequence'
                   WHEN 'v' THEN 'view'
                   WHEN 'm' THEN 'materialized_view'
               END AS object_kind,
               count(*)
          FROM pg_class AS c
          JOIN pg_namespace AS n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relkind IN ('r', 'p', 'i', 'I', 'S', 'v', 'm')
         GROUP BY object_kind
         ORDER BY object_kind
    """
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            rows = connection.execute(query).fetchall()
    except psycopg.Error as exc:
        raise click.ClickException(f"Could not collect PostgreSQL schema inventory: {exc}") from exc
    return {str(kind): int(count) for kind, count in rows}


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
        if MANAGED_PRINCIPAL_RE.match(str(row[0])) is None
    ]


def drain_ephemeral_principals(database_url: str) -> int:
    """Revoke every scoped login and prove the reserved login namespace is empty."""
    try:
        with psycopg.connect(database_url, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn:
            schema_row = conn.execute("SELECT current_schema()").fetchone()
            auth_schema = auth_schema_for(None if schema_row is None else schema_row[0])
            row = conn.execute(f"SELECT {auth_schema}.drain_ephemeral_principals()").fetchone()
            if row is None:
                raise click.ClickException("Managed-role drain returned no result")
            drained = int(row[0])
            remaining = conn.execute(_EPHEMERAL_ROLE_SQL).fetchall()
    except psycopg.Error as exc:
        raise click.ClickException(f"Unable to drain scoped PostgreSQL roles: {exc}") from exc
    if drained < 0 or remaining:
        raise click.ClickException(
            "Hub backup aborted: an ephemeral PostgreSQL login remains after agent-role drain"
        )
    return drained


def reconcile_restored_principals(database_url: str) -> int:
    """Remove restored reserved-prefix roles and retire their restored bindings."""
    return drain_ephemeral_principals(database_url)


def restore_postgres_globals(database_url: str, globals_path: Path) -> None:
    """Replay verified stable cluster globals into the managed PostgreSQL container."""
    require_regular_file(globals_path, label="PostgreSQL globals")
    user = _dsn_user(database_url) or DEFAULT_POSTGRES_USER
    container = _managed_postgres_container(database_url)
    with open_regular_binary(globals_path, label="PostgreSQL globals") as globals_file:
        replay = _idempotent_global_role_creates(globals_file.read())
        ensure_docker_allowed("hub backup PostgreSQL globals restore", runner=subprocess.run)
        result = subprocess.run(  # nosec B603 - fixed docker/psql argv and verified file input
            _postgres_client_command(
                "psql",
                "-U",
                user,
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                interactive=True,
                container=container,
            ),
            input=replay,
            capture_output=True,
            check=False,
            timeout=_docker_pg_dump_timeout_seconds(),
        )
    _raise_for_subprocess_error(result, "Docker psql globals restore")


def _idempotent_global_role_creates(script: bytes) -> bytes:
    """Wrap generated role creation so stable roles can already exist on restore."""
    replay = _ROLE_GRANTOR_RE.sub(b";", script)
    if _ROLE_CREATE_LINE_RE.search(replay) is None:
        return replay
    tag_index = 0
    tag = b"$gobby_role_0$"
    while tag in replay:
        tag_index += 1
        tag = f"$gobby_role_{tag_index}$".encode()

    def wrap(match: re.Match[bytes]) -> bytes:
        return b"\n".join(
            (
                b"DO " + tag,
                b"BEGIN",
                b"    " + match.group(0),
                b"EXCEPTION WHEN duplicate_object THEN",
                b"    NULL;",
                b"END",
                tag + b";",
            )
        )

    return _ROLE_CREATE_LINE_RE.sub(wrap, replay)


def dump_postgres(
    database_url: str,
    backup_root: Path,
) -> tuple[list[ArtifactRecord], dict[str, object]]:
    """Dump the database and cluster globals, then check the archive is readable.

    The dump deliberately keeps ownership and ACLs: roles are restored from the
    globals dump first, so a stripped dump would silently drop privileges.
    """
    drain_ephemeral_principals(database_url)
    user = _dsn_user(database_url) or DEFAULT_POSTGRES_USER
    database = _dsn_db(database_url) or DEFAULT_POSTGRES_DB
    container = _managed_postgres_container(database_url)
    postgres_version = _server_version(database_url)
    dump_timeout = _docker_pg_dump_timeout_seconds()

    dump_path = _prepare_artifact_path(backup_root, POSTGRES_DUMP_RELPATH)
    _capture_stdout(
        _postgres_client_command(
            "pg_dump",
            "-U",
            user,
            "-d",
            database,
            "-Fc",
            container=container,
        ),
        dump_path,
        action="Docker pg_dump",
        timeout=dump_timeout,
    )

    globals_path = _prepare_artifact_path(backup_root, GLOBALS_DUMP_RELPATH)
    _capture_stdout(
        _postgres_client_command(
            "pg_dumpall",
            "-U",
            user,
            "--globals-only",
            container=container,
        ),
        globals_path,
        action="Docker pg_dumpall --globals-only",
        timeout=dump_timeout,
    )

    _check_archive_readable(dump_path, container=container)

    artifacts = [
        _artifact_record("postgres-dump", backup_root, POSTGRES_DUMP_RELPATH),
        _artifact_record("postgres-globals", backup_root, GLOBALS_DUMP_RELPATH),
    ]
    details: dict[str, object] = {
        "postgres_version": postgres_version,
        "archive_list_checked": True,
    }
    return artifacts, details


def _postgres_client_command(
    client: str,
    *args: str,
    interactive: bool = False,
    container: str = POSTGRES_CONTAINER,
) -> list[str]:
    command = ["docker", "exec"]
    if os.environ.get(MAINTENANCE_EPOCH_ENV):
        command.extend(["-e", "PGOPTIONS"])
    if interactive:
        command.append("-i")
    return [*command, container, client, *args]


def _server_version(database_url: str) -> str:
    try:
        with psycopg.connect(database_url, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn:
            row = conn.execute("SHOW server_version").fetchone()
    except psycopg.Error as exc:
        raise click.ClickException(f"Unable to read PostgreSQL server version: {exc}") from exc
    return str(row[0]) if row else "unknown"


def _check_archive_readable(
    dump_path: Path,
    *,
    container: str = POSTGRES_CONTAINER,
) -> None:
    if not dump_path.is_file():
        raise click.ClickException(f"PostgreSQL dump was not created: {dump_path}")
    command = ["docker", "exec", "-i", container, "pg_restore", "--list"]
    ensure_docker_allowed("hub backup PostgreSQL archive check", runner=subprocess.run)
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
        content_sha256 = qdrant_collection_digest(client, name)
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
        collections[name] = {
            "points": points,
            "snapshot": relpath,
            "content_sha256": content_sha256,
        }

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
            with open_exclusive_binary(
                destination, label=f"Qdrant snapshot for {collection}"
            ) as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"Qdrant snapshot download failed for collection {collection}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# FalkorDB
# ---------------------------------------------------------------------------


def dump_falkordb(
    backup_root: Path,
    *,
    container: str = FALKORDB_CONTAINER,
) -> tuple[list[ArtifactRecord], dict[str, object]]:
    """Force a fresh RDB save and copy it out of the FalkorDB container."""
    previous_save = _falkordb_lastsave(container)
    _redis_cli(container, _REDIS_BGSAVE)
    _await_falkordb_bgsave(container, previous_save)

    graphs = sorted(
        line.strip()
        for line in _redis_cli(container, _REDIS_GRAPH_LIST).splitlines()
        if line.strip()
    )
    graph_inventory = {graph: _falkordb_graph_counts(container, graph) for graph in graphs}
    dbsize = _parse_int(_redis_cli(container, _REDIS_DBSIZE), what="FalkorDB DBSIZE")

    destination = _prepare_artifact_path(backup_root, FALKORDB_DUMP_RELPATH)
    _copy_from_container(f"{container}:{_FALKORDB_RDB_PATH}", destination)

    artifacts = [_artifact_record("falkordb-rdb", backup_root, FALKORDB_DUMP_RELPATH)]
    return artifacts, {"graphs": graphs, "graph_inventory": graph_inventory, "dbsize": dbsize}


def _falkordb_graph_counts(container: str, graph: str) -> dict[str, int]:
    graph_arg = shlex.quote(graph)
    node_query = shlex.quote("MATCH (n) RETURN count(n)")
    edge_query = shlex.quote("MATCH ()-[r]->() RETURN count(r)")
    nodes = _parse_graph_count(
        _redis_cli(container, f"GRAPH.QUERY {graph_arg} {node_query} --compact"),
        graph=graph,
        metric="nodes",
    )
    edges = _parse_graph_count(
        _redis_cli(container, f"GRAPH.QUERY {graph_arg} {edge_query} --compact"),
        graph=graph,
        metric="edges",
    )
    return {"nodes": nodes, "edges": edges}


def _parse_graph_count(payload: str, *, graph: str, metric: str) -> int:
    for line in payload.splitlines():
        try:
            return int(line.strip())
        except ValueError:
            continue
    raise click.ClickException(f"FalkorDB graph inventory returned no {metric} count for {graph}")


def _await_falkordb_bgsave(container: str, previous_save: int) -> None:
    deadline = time.monotonic() + FALKORDB_BGSAVE_TIMEOUT_SECONDS
    while True:
        fields = _parse_redis_info(_redis_cli(container, _REDIS_INFO_PERSISTENCE))
        current_save = _falkordb_lastsave(container)
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


def _falkordb_lastsave(container: str) -> int:
    return _parse_int(_redis_cli(container, _REDIS_LASTSAVE), what="FalkorDB LASTSAVE")


def _redis_cli(container: str, request: str) -> str:
    """Run a fixed redis-cli request inside the FalkorDB container.

    The password stays inside the container: `sh -c` expands the container's
    own environment variable, so it never enters this process's argv.
    """
    command = [
        "docker",
        "exec",
        container,
        "sh",
        "-c",
        f'redis-cli -a "$GOBBY_FALKORDB_PASSWORD" --no-auth-warning --raw {request}',
    ]
    action = f"FalkorDB {request}"
    ensure_docker_allowed("hub backup FalkorDB command", runner=subprocess.run)
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
    ensure_docker_allowed("hub backup container copy", runner=subprocess.run)
    try:
        result = subprocess.run(  # nosec B603
            command,
            capture_output=True,
            timeout=_DOCKER_CP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        raise click.ClickException(f"Docker cp of {source} failed: {exc}") from exc
    _raise_for_subprocess_error(result, f"Docker cp of {source}")
    require_regular_file(destination, label="Docker cp artifact")
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
    source_inventories: dict[str, dict[str, object]] = {}
    for volume in volumes:
        source_inventories[volume] = _source_volume_inventory(volume)
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
        ensure_docker_allowed("hub backup volume archive", runner=subprocess.run)
        try:
            result = subprocess.run(  # nosec B603
                command,
                capture_output=True,
                timeout=_VOLUME_ARCHIVE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
            raise click.ClickException(f"{action} failed: {exc}") from exc
        _raise_for_subprocess_error(result, action)
        require_regular_file(destination, label=action)
        destination.chmod(0o600)
        artifacts.append(_artifact_record(f"volume-{volume}", backup_root, relpath))
        archived.append(volume)
    return artifacts, {"volumes": archived, "source_inventories": source_inventories}


def _source_volume_inventory(volume: str) -> dict[str, object]:
    command = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{volume}:/source:ro",
        "alpine",
        "tar",
        "cf",
        "-",
        "-C",
        "/source",
        ".",
    ]
    action = f"Docker source inventory for volume {volume}"
    ensure_docker_allowed("hub backup volume inventory", runner=subprocess.run)
    with tempfile.TemporaryFile() as stream:
        try:
            result = subprocess.run(  # nosec B603
                command,
                stdout=stream,
                stderr=subprocess.PIPE,
                timeout=_VOLUME_ARCHIVE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
            raise click.ClickException(f"{action} failed: {exc}") from exc
        _raise_for_subprocess_error(result, action)
        stream.seek(0)
        return tar_stream_inventory(stream, label=action).to_dict()


def archive_rule_allow_audit_logs(
    logs_dir: Path,
    backup_root: Path,
) -> list[ArtifactRecord]:
    """Copy the active allow audit log and numeric rotations into the backup."""
    refuse_symlink_traversal(logs_dir, label="Allow-audit log source")
    try:
        mode = logs_dir.lstat().st_mode
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(mode):
        raise click.ClickException(f"Allow-audit log source is not a directory: {logs_dir}")

    artifacts: list[ArtifactRecord] = []
    prefix = f"{RULE_ALLOW_AUDIT_LOG_FILENAME}."
    for source in sorted(logs_dir.iterdir(), key=lambda path: path.name):
        is_rotation = source.name.startswith(prefix) and source.name.removeprefix(prefix).isdigit()
        if source.name != RULE_ALLOW_AUDIT_LOG_FILENAME and not is_rotation:
            continue
        require_regular_file(source, label="Allow-audit log source")
        relpath = f"{ALLOW_AUDIT_ARCHIVE_DIR}/{source.name}"
        destination = _prepare_artifact_path(backup_root, relpath)
        with open_regular_binary(source, label="Allow-audit log source") as source_stream:
            output = open_exclusive_binary(destination, label="Allow-audit log artifact")
            with output:
                while chunk := source_stream.read(1024 * 1024):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        artifacts.append(_artifact_record(f"rule_allow_audit:{source.name}", backup_root, relpath))
    return artifacts


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _capture_stdout(command: list[str], destination: Path, *, action: str, timeout: int) -> None:
    ensure_docker_allowed(action, runner=subprocess.run)
    try:
        with open_exclusive_binary(destination, label=action) as output:
            result = subprocess.run(  # nosec B603
                command,
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
            output.flush()
            os.fsync(output.fileno())
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        raise click.ClickException(f"{action} failed: {exc}") from exc
    _raise_for_subprocess_error(result, action)
    destination.chmod(0o600)


def _prepare_artifact_path(backup_root: Path, relpath: str) -> Path:
    """Create the artifact's parent directories as 0700 and return its path."""
    relative = Path(relpath)
    if relative.is_absolute() or ".." in relative.parts:
        raise click.ClickException(f"Unsafe backup artifact path: {relpath!r}")
    refuse_symlink_traversal(backup_root, label="Backup artifact destination")
    destination = backup_root / relpath
    destination.parent.mkdir(parents=True, exist_ok=True)
    refuse_symlink_traversal(destination, label="Backup artifact destination")
    try:
        destination.lstat()
    except FileNotFoundError:
        pass
    else:
        raise click.ClickException(f"Backup artifact destination already exists: {destination}")
    directory = backup_root
    directory.chmod(0o700)
    for part in Path(relpath).parent.parts:
        directory = directory / part
        directory.chmod(0o700)
    return destination


def _artifact_record(name: str, backup_root: Path, relpath: str) -> ArtifactRecord:
    path = backup_root / relpath
    sha256, size = file_digest(path, label=f"backup artifact {relpath}")
    return ArtifactRecord(
        name=name,
        path=relpath,
        sha256=sha256,
        size_bytes=size,
    )


def _quote_identifier(identifier: str) -> str:
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def _parse_int(payload: str, *, what: str) -> int:
    try:
        return int(payload.strip())
    except ValueError as exc:
        raise click.ClickException(f"{what} returned a non-numeric reply: {payload!r}") from exc
