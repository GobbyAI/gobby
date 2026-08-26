"""Scratch-restore verification for hub backups.

`restore_verified` is earned by rebuilding payloads in throwaway containers or
directories and comparing source inventories: PostgreSQL per-table rows plus
public schema-object counts, Qdrant point ids/payloads/vectors, FalkorDB graph
node/edge counts, and volume path/type/regular-file byte digests. These probes do
not prove PostgreSQL row values, FalkorDB property values, or volume ownership,
mode, timestamps, links, and special files; volume links and special files are
refused. Archive readability alone never counts.
"""

from __future__ import annotations

import json
import secrets
import shlex
import shutil
import subprocess  # nosec B404 # fixed docker argv, no shell
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

import click
from qdrant_client import QdrantClient

from gobby.cli.hub_backup._content import (
    ContentInventory,
    qdrant_collection_digest,
)
from gobby.cli.hub_backup._content import (
    archive_inventory as _archive_inventory,
)
from gobby.cli.hub_backup._integrity import (
    open_exclusive_binary,
    open_regular_binary,
    require_regular_file,
)
from gobby.cli.hub_backup._manifest import VerificationState
from gobby.cli.hub_backup._stores import MANAGED_PRINCIPAL_RE
from gobby.cli.installers.docker_guard import ensure_docker_allowed
from gobby.cli.postgres_backup import _process_output, _raise_for_subprocess_error

POSTGRES_VERIFY_IMAGE = "gobby-postgres-local:18-pgsearch"
FALKORDB_VERIFY_IMAGE = "falkordb/falkordb:latest"

_PG_METHOD = "scratch-pg-restore+roles+rows+schema-counts"
_QDRANT_METHOD = "qdrant-scratch-recover+point-content-digest"
_FALKOR_METHOD = "falkordb-scratch-rdb-load+graph-counts"
_VOLUME_METHOD = "tar-extract+source-content-inventory"

_QDRANT_SCRATCH_PREFIX = "hub_backup_verify_"
_QDRANT_TIMEOUT_SECONDS = 120
_SCRATCH_DB = "gobby"
_SCRATCH_REPAIR_PGOPTIONS = "PGOPTIONS=-c event_triggers=off"
_SUPERUSER = "postgres"
_FALKOR_DATA_DIR = "/var/lib/falkordb/data"
_REDIS_CLI = 'redis-cli -a "$GOBBY_FALKORDB_PASSWORD" --no-auth-warning --raw'
_DISPOSABLE_LABEL = "io.gobby.disposable"
_DISPOSABLE_RUN_LABEL = "io.gobby.run-id"
_COMPOSE_LABEL_PREFIX = "com.docker.compose."

_DOCKER_TIMEOUT_SECONDS = 600
_DOCKER_RM_TIMEOUT_SECONDS = 60
_PROBE_TIMEOUT_SECONDS = 30
_PG_READY_TIMEOUT_SECONDS = 120
_FALKOR_READY_TIMEOUT_SECONDS = 60
_POLL_INTERVAL_SECONDS = 1.0

_ROLE_QUERY = "SELECT rolname, rolsuper, rolcanlogin FROM pg_roles WHERE rolname NOT LIKE 'pg\\_%'"


@dataclass(frozen=True)
class RoleExpectation:
    """A role the restored cluster must carry, with its login/superuser bits."""

    rolname: str
    rolsuper: bool
    rolcanlogin: bool


@dataclass(frozen=True)
class DisposableContainer:
    """Immutable capability authorizing cleanup of one scratch container."""

    name: str
    container_id: str
    run_id: str


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


def verify_postgres_restore(
    dump_path: Path,
    globals_path: Path,
    *,
    expected_probes: dict[str, int],
    expected_roles: list[RoleExpectation],
    expected_schema_objects: dict[str, int] | None = None,
) -> tuple[VerificationState, dict[str, object]]:
    """Restore dump + globals into a throwaway cluster and prove roles and row counts."""
    _require_file(dump_path, "PostgreSQL dump")
    _require_file(globals_path, "PostgreSQL globals")

    container = f"gobby-hub-verify-pg-{uuid.uuid4().hex[:8]}"
    disposable: DisposableContainer | None = None
    skipped_principals: list[str] = []
    try:
        disposable = _start_scratch_postgres(container)
        _wait_for_postgres(container)
        _replay_globals(container, globals_path)
        skipped_principals = _check_roles(container, expected_roles)
        _restore_dump(container, dump_path)
        _check_row_counts(container, expected_probes)
        if expected_schema_objects is not None:
            _check_schema_object_counts(container, expected_schema_objects)
    finally:
        if disposable is not None:
            _remove_container(disposable)

    details: dict[str, object] = {
        "tables_checked": len(expected_probes),
        "roles_checked": len(expected_roles) - len(skipped_principals),
        "scratch_container": container,
    }
    if skipped_principals:
        details["managed_principals_skipped"] = skipped_principals
    if expected_schema_objects is not None:
        details["schema_objects_checked"] = len(expected_schema_objects)
    return _verified(_PG_METHOD), details


def _start_scratch_postgres(container: str) -> DisposableContainer:
    password = secrets.token_urlsafe(24)
    run_id = uuid.uuid4().hex
    result = _docker(
        "run",
        "-d",
        "--name",
        container,
        "--label",
        f"{_DISPOSABLE_LABEL}=true",
        "--label",
        f"{_DISPOSABLE_RUN_LABEL}={run_id}",
        "-e",
        f"POSTGRES_PASSWORD={password}",
        "-e",
        f"POSTGRES_USER={_SUPERUSER}",
        "-e",
        f"POSTGRES_DB={_SUPERUSER}",
        POSTGRES_VERIFY_IMAGE,
        "postgres",
        "-c",
        "shared_preload_libraries=pg_search,pgaudit",
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )
    _raise_for_subprocess_error(result, "Scratch PostgreSQL container start")
    container_id = _process_output(result.stdout)
    if not container_id:
        raise click.ClickException("Scratch PostgreSQL container start returned no container ID")
    return DisposableContainer(name=container, container_id=container_id, run_id=run_id)


def _wait_for_postgres(container: str) -> None:
    def _ready() -> bool:
        result = _docker(
            "exec",
            container,
            "pg_isready",
            "-U",
            _SUPERUSER,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        return result.returncode == 0

    if not _poll(_ready, timeout=_PG_READY_TIMEOUT_SECONDS):
        raise click.ClickException(
            f"Scratch PostgreSQL container {container} never became ready "
            f"within {_PG_READY_TIMEOUT_SECONDS}s; restore verification failed"
        )


def _replay_globals(container: str, globals_path: Path) -> None:
    # Deliberately no ON_ERROR_STOP: pg_dumpall globals re-create bootstrap roles
    # that already exist in a fresh cluster. Verification is outcome-based — the
    # role/ACL check below is what proves the replay worked.
    with open_regular_binary(globals_path, label="PostgreSQL globals") as handle:
        _docker(
            "exec",
            "-i",
            container,
            "psql",
            "-U",
            _SUPERUSER,
            "-d",
            _SUPERUSER,
            "-f",
            "-",
            timeout=_DOCKER_TIMEOUT_SECONDS,
            stdin=handle,
        )


def _check_roles(container: str, expected_roles: list[RoleExpectation]) -> list[str]:
    """Prove the expected roles exist with matching bits; return skipped managed principals.

    Managed principals (agent, interactive, and maintenance generations) are revoked by
    the pre-dump drain, so one that still reached the expectations is reported by name
    instead of failing verification. Any other missing role is fatal.
    """
    if not expected_roles:
        return []
    output = _psql_query(container, _SUPERUSER, _ROLE_QUERY, action="Scratch role query")
    actual: dict[str, tuple[bool, bool]] = {}
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        actual[fields[0].strip()] = (fields[1].strip() == "t", fields[2].strip() == "t")

    problems: list[str] = []
    skipped: list[str] = []
    for role in expected_roles:
        if role.rolname not in actual:
            if MANAGED_PRINCIPAL_RE.match(role.rolname) is not None:
                skipped.append(role.rolname)
                continue
            problems.append(f"{role.rolname} (missing from restored cluster)")
            continue
        if role.rolname == _SUPERUSER:
            # Bootstrap role attributes are owned by the scratch image, not the dump.
            continue
        is_super, can_login = actual[role.rolname]
        if is_super != role.rolsuper:
            problems.append(f"{role.rolname} (rolsuper: expected {role.rolsuper}, got {is_super})")
        if can_login != role.rolcanlogin:
            problems.append(
                f"{role.rolname} (rolcanlogin: expected {role.rolcanlogin}, got {can_login})"
            )
    if problems:
        raise click.ClickException(
            "Scratch restore role/ACL verification failed: " + "; ".join(problems)
        )
    return skipped


def _restore_dump(container: str, dump_path: Path) -> None:
    created = _docker(
        "exec",
        container,
        "createdb",
        "-U",
        _SUPERUSER,
        _SCRATCH_DB,
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )
    _raise_for_subprocess_error(created, "Scratch createdb")

    # No --no-owner: the dump carries ownership and the roles exist from the
    # globals replay, so a clean restore also proves ownership is restorable.
    with open_regular_binary(dump_path, label="PostgreSQL dump") as handle:
        result = _docker(
            "exec",
            "-i",
            container,
            "pg_restore",
            "-U",
            _SUPERUSER,
            "-d",
            _SCRATCH_DB,
            timeout=_DOCKER_TIMEOUT_SECONDS,
            stdin=handle,
        )
    if result.returncode != 0:
        raise click.ClickException(
            f"Scratch pg_restore failed (exit {result.returncode}): {_stderr_tail(result)}"
        )


def _check_row_counts(container: str, expected_probes: dict[str, int]) -> None:
    mismatches: list[str] = []
    for table, expected in expected_probes.items():
        query = f"SELECT count(*) FROM {_quote_identifier(table)}"
        output = _psql_query(
            container,
            _SCRATCH_DB,
            query,
            action=f"Scratch row count for {table}",
        )
        actual = _parse_count(output)
        if actual != expected:
            mismatches.append(f"{table} (expected {expected}, got {actual})")
    if mismatches:
        raise click.ClickException(
            "Scratch restore row-count verification failed: " + "; ".join(mismatches)
        )


def _check_schema_object_counts(container: str, expected: dict[str, int]) -> None:
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
    output = _psql_query(
        container,
        _SCRATCH_DB,
        query,
        action="Scratch schema object inventory",
    )
    actual: dict[str, int] = {}
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 2:
            continue
        try:
            actual[fields[0]] = int(fields[1])
        except ValueError:
            continue
    if actual != expected:
        keys = sorted(set(expected) | set(actual))
        mismatches = [
            f"{key} (expected {expected.get(key, 0)}, got {actual.get(key, 0)})"
            for key in keys
            if expected.get(key, 0) != actual.get(key, 0)
        ]
        raise click.ClickException(
            "Scratch restore schema object verification failed: " + "; ".join(mismatches)
        )


def _psql_query(container: str, database: str, sql: str, *, action: str) -> str:
    docker_options = ["-e", _SCRATCH_REPAIR_PGOPTIONS] if database == _SCRATCH_DB else []
    result = _docker(
        "exec",
        *docker_options,
        container,
        "psql",
        "-U",
        _SUPERUSER,
        "-d",
        database,
        "-t",
        "-A",
        "-F",
        "\t",
        "-c",
        sql,
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )
    _raise_for_subprocess_error(result, action)
    return _process_output(result.stdout)


def _parse_count(output: str) -> int | None:
    try:
        return int(output.strip().splitlines()[0])
    except (IndexError, ValueError):
        return None


def _quote_identifier(name: str) -> str:
    return ".".join('"' + part.replace('"', '""') + '"' for part in name.split("."))


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------


def verify_qdrant_restore(
    url: str | None,
    api_key: str | None,
    snapshots: dict[str, Path],
    expected_counts: dict[str, int],
    expected_digests: dict[str, str],
) -> tuple[VerificationState, dict[str, object]]:
    """Recover snapshots and prove point counts plus canonical point content."""
    if not url:
        raise click.ClickException(
            "Qdrant restore verification requires a remote Qdrant url; "
            "embedded/local mode cannot recover snapshots"
        )
    for collection, snapshot_path in snapshots.items():
        _require_file(snapshot_path, f"Qdrant snapshot for {collection}")

    client = QdrantClient(url=url, api_key=api_key, timeout=_QDRANT_TIMEOUT_SECONDS)
    verified: dict[str, int] = {}
    verified_digests: dict[str, str] = {}
    try:
        for collection, snapshot_path in sorted(snapshots.items()):
            if collection not in expected_counts:
                raise click.ClickException(
                    f"Qdrant restore verification failed: no expected point count "
                    f"recorded for collection {collection}"
                )
            if collection not in expected_digests:
                raise click.ClickException(
                    f"Qdrant restore verification failed: no source content digest "
                    f"recorded for collection {collection}"
                )
            scratch = f"{_QDRANT_SCRATCH_PREFIX}{collection}"
            try:
                _recover_qdrant_collection(client, scratch, snapshot_path)
                actual = client.count(scratch, exact=True).count
                actual_digest = qdrant_collection_digest(client, scratch)
            finally:
                _delete_qdrant_collection(client, scratch)
            expected = expected_counts[collection]
            if actual != expected:
                raise click.ClickException(
                    f"Qdrant restore verification failed for collection {collection}: "
                    f"expected {expected} points, got {actual}"
                )
            expected_digest = expected_digests[collection]
            if actual_digest != expected_digest:
                raise click.ClickException(
                    f"Qdrant restore verification failed for collection {collection}: "
                    f"source content digest {expected_digest}, restored {actual_digest}"
                )
            verified[collection] = actual
            verified_digests[collection] = actual_digest
    finally:
        _close_qdrant_client(client)

    details: dict[str, object] = {
        "collections_checked": len(verified),
        "points_verified": verified,
        "content_digests_verified": verified_digests,
    }
    return _verified(_QDRANT_METHOD), details


def _recover_qdrant_collection(client: QdrantClient, scratch: str, snapshot_path: Path) -> None:
    try:
        with snapshot_path.open("rb") as handle:
            client.http.snapshots_api.recover_from_uploaded_snapshot(
                collection_name=scratch,
                snapshot=handle,
                wait=True,
            )
    except OSError as exc:
        raise click.ClickException(
            f"Qdrant snapshot upload failed for scratch collection {scratch}: {exc}"
        ) from exc


def _delete_qdrant_collection(client: QdrantClient, scratch: str) -> None:
    """Drop the scratch collection after its uploaded snapshot files.

    Qdrant keeps an uploaded snapshot under ``snapshots/<collection>/`` even after
    the collection is deleted, so the snapshots go first or every backup leaves
    the full snapshot set behind in the Qdrant container.
    """
    try:
        for snapshot in client.list_snapshots(scratch):
            client.delete_snapshot(collection_name=scratch, snapshot_name=snapshot.name, wait=True)
    except Exception as exc:  # cleanup must never mask the verification outcome
        click.echo(f"Warning: failed to delete scratch snapshots for {scratch}: {exc}", err=True)
    try:
        client.delete_collection(scratch)
    except Exception as exc:  # cleanup must never mask the verification outcome
        click.echo(f"Warning: failed to delete scratch collection {scratch}: {exc}", err=True)


def _close_qdrant_client(client: QdrantClient) -> None:
    try:
        client.close()
    except Exception as exc:  # cleanup must never mask the verification outcome
        click.echo(f"Warning: failed to close Qdrant verification client: {exc}", err=True)


# ---------------------------------------------------------------------------
# FalkorDB
# ---------------------------------------------------------------------------


def verify_falkordb_restore(
    rdb_path: Path,
    expected_inventory: dict[str, dict[str, int]],
) -> tuple[VerificationState, dict[str, object]]:
    """Boot a scratch FalkorDB and compare each graph's node and edge counts."""
    _require_file(rdb_path, "FalkorDB RDB")

    container = f"gobby-hub-verify-falkor-{uuid.uuid4().hex[:8]}"
    disposable: DisposableContainer | None = None
    scratch_dir = Path(tempfile.mkdtemp(prefix="gobby-hub-verify-falkor-")).resolve(strict=True)
    try:
        scratch_dir.chmod(0o700)
        # FalkorDB loads its RDB at startup only, so the copy must be in place first.
        with open_regular_binary(rdb_path, label="FalkorDB RDB") as source:
            with open_exclusive_binary(
                scratch_dir / "dump.rdb", label="FalkorDB scratch RDB"
            ) as destination:
                while chunk := source.read(1024 * 1024):
                    destination.write(chunk)
        disposable = _start_scratch_falkordb(container, scratch_dir)
        _wait_for_falkordb(container)
        actual = set(_list_graphs(container))
        expected = set(expected_inventory)
        if actual != expected:
            raise click.ClickException(
                "FalkorDB restore verification failed: "
                f"missing {sorted(expected - actual)}, unexpected {sorted(actual - expected)}"
            )
        restored_inventory = {graph: _graph_counts(container, graph) for graph in sorted(actual)}
        if restored_inventory != expected_inventory:
            mismatches = [
                f"{graph} {metric} (expected {expected_inventory[graph][metric]}, "
                f"got {restored_inventory[graph][metric]})"
                for graph in sorted(expected)
                for metric in ("nodes", "edges")
                if restored_inventory[graph][metric] != expected_inventory[graph][metric]
            ]
            raise click.ClickException(
                "FalkorDB restore content verification failed: " + "; ".join(mismatches)
            )
    finally:
        if disposable is not None:
            _remove_container(disposable)
        shutil.rmtree(scratch_dir, ignore_errors=True)

    details: dict[str, object] = {
        "graphs_verified": len(expected_inventory),
        "graph_inventory_verified": expected_inventory,
    }
    return _verified(_FALKOR_METHOD), details


def _start_scratch_falkordb(
    container: str,
    scratch_dir: Path,
) -> DisposableContainer:
    password = secrets.token_urlsafe(24)
    run_id = uuid.uuid4().hex
    result = _docker(
        "run",
        "-d",
        "--name",
        container,
        "--label",
        f"{_DISPOSABLE_LABEL}=true",
        "--label",
        f"{_DISPOSABLE_RUN_LABEL}={run_id}",
        "-e",
        f"REDIS_ARGS=--requirepass {password}",
        "-e",
        f"GOBBY_FALKORDB_PASSWORD={password}",
        "-v",
        f"{scratch_dir}:{_FALKOR_DATA_DIR}",
        FALKORDB_VERIFY_IMAGE,
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )
    _raise_for_subprocess_error(result, "Scratch FalkorDB container start")
    container_id = _process_output(result.stdout)
    if not container_id:
        raise click.ClickException("Scratch FalkorDB container start returned no container ID")
    return DisposableContainer(name=container, container_id=container_id, run_id=run_id)


def _wait_for_falkordb(container: str) -> None:
    def _ready() -> bool:
        result = _redis_cli(container, "PING", timeout=_PROBE_TIMEOUT_SECONDS)
        return result.returncode == 0 and _process_output(result.stdout).upper() == "PONG"

    if not _poll(_ready, timeout=_FALKOR_READY_TIMEOUT_SECONDS):
        raise click.ClickException(
            f"Scratch FalkorDB container {container} never became ready "
            f"within {_FALKOR_READY_TIMEOUT_SECONDS}s; restore verification failed"
        )


def _list_graphs(container: str) -> list[str]:
    result = _redis_cli(container, "GRAPH.LIST", timeout=_DOCKER_TIMEOUT_SECONDS)
    _raise_for_subprocess_error(result, "Scratch GRAPH.LIST")
    return [line.strip() for line in _process_output(result.stdout).splitlines() if line.strip()]


def _graph_counts(container: str, graph: str) -> dict[str, int]:
    graph_arg = shlex.quote(graph)
    node_query = shlex.quote("MATCH (n) RETURN count(n)")
    edge_query = shlex.quote("MATCH ()-[r]->() RETURN count(r)")
    nodes = _graph_count_query(container, graph_arg, node_query, graph=graph, metric="nodes")
    edges = _graph_count_query(container, graph_arg, edge_query, graph=graph, metric="edges")
    return {"nodes": nodes, "edges": edges}


def _graph_count_query(
    container: str,
    graph_arg: str,
    query_arg: str,
    *,
    graph: str,
    metric: str,
) -> int:
    result = _redis_cli(
        container,
        f"GRAPH.QUERY {graph_arg} {query_arg} --compact",
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )
    _raise_for_subprocess_error(result, f"Scratch GRAPH.QUERY {graph} {metric}")
    for line in _process_output(result.stdout).splitlines():
        try:
            return int(line.strip())
        except ValueError:
            continue
    raise click.ClickException(f"Scratch GRAPH.QUERY returned no {metric} count for {graph}")


def _redis_cli(container: str, command: str, *, timeout: int) -> subprocess.CompletedProcess[bytes]:
    # `sh -c` so the password expands from the container environment; it is never
    # resolved into argv on this side.
    return _docker(
        "exec",
        container,
        "sh",
        "-c",
        f"{_REDIS_CLI} {command}",
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Volume archives
# ---------------------------------------------------------------------------


def verify_volume_archives(
    archives: dict[str, Path],
    expected_inventories: dict[str, dict[str, object]],
) -> tuple[VerificationState, dict[str, object]]:
    """Extract archives and compare path/type/file-byte inventories to the source."""
    for volume, archive_path in archives.items():
        _require_file(archive_path, f"Volume archive for {volume}")

    counts: dict[str, int] = {}
    verified_inventories: dict[str, dict[str, object]] = {}
    for volume, archive_path in sorted(archives.items()):
        expected = _expected_content_inventory(volume, expected_inventories)
        actual = _archive_inventory(archive_path, label=f"Volume archive for {volume}")
        if actual != expected:
            raise click.ClickException(
                f"Volume archive {volume} content inventory differs from source: "
                f"expected {expected.to_dict()}, got {actual.to_dict()}"
            )
        scratch_dir = Path(tempfile.mkdtemp(prefix="gobby-hub-verify-vol-")).resolve(strict=True)
        try:
            counts[volume] = _extract_archive(volume, archive_path, scratch_dir)
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        verified_inventories[volume] = actual.to_dict()

    details: dict[str, object] = {
        "archives": counts,
        "source_inventories_verified": verified_inventories,
    }
    return _verified(_VOLUME_METHOD), details


def _extract_archive(volume: str, archive_path: Path, scratch_dir: Path) -> int:
    try:
        with open_regular_binary(archive_path, label=f"Volume archive for {volume}") as source:
            with tarfile.open(fileobj=source, mode="r:gz") as tar:
                tar.extractall(  # nosec B202 # data filter applied
                    path=scratch_dir,
                    filter="data",
                )
                members = tar.getmembers()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise click.ClickException(
            f"Volume archive {volume} failed scratch extraction: {exc}"
        ) from exc
    if not members:
        raise click.ClickException(
            f"Volume archive {volume} extracted 0 members; archive is empty or corrupt"
        )
    return len(members)


def _expected_content_inventory(
    volume: str,
    inventories: dict[str, dict[str, object]],
) -> ContentInventory:
    record = inventories.get(volume)
    if not isinstance(record, dict):
        raise click.ClickException(
            f"Volume restore verification has no source inventory for {volume}"
        )
    members = record.get("members")
    sha256 = record.get("sha256")
    if not isinstance(members, int) or not isinstance(sha256, str):
        raise click.ClickException(f"Volume source inventory for {volume} is incomplete")
    return ContentInventory(members=members, sha256=sha256)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _docker(
    *argv: str,
    timeout: int,
    stdin: IO[bytes] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    command = ["docker", *argv]
    ensure_docker_allowed("hub backup restore verification", runner=subprocess.run)
    try:
        return subprocess.run(  # nosec B603
            command,
            stdin=stdin,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        raise click.ClickException(f"docker {' '.join(argv[:3])} failed: {exc}") from exc


def _poll(
    probe: Callable[[], bool],
    *,
    timeout: float,
    interval: float = _POLL_INTERVAL_SECONDS,
) -> bool:
    attempts = max(1, int(timeout / interval))
    deadline = time.monotonic() + timeout
    for attempt in range(attempts):
        if probe():
            return True
        if attempt + 1 >= attempts or time.monotonic() >= deadline:
            break
        time.sleep(interval)
    return False


def _remove_container(container: DisposableContainer) -> None:
    if not _has_disposal_capability(container):
        return
    try:
        result = _docker(
            "rm",
            "-f",
            "-v",
            container.container_id,
            timeout=_DOCKER_RM_TIMEOUT_SECONDS,
        )
    except click.ClickException as exc:
        click.echo(f"Warning: failed to remove scratch container {container.name}: {exc}", err=True)
        return
    if result.returncode != 0:
        click.echo(
            f"Warning: failed to remove scratch container {container.name}: {_stderr_tail(result)}",
            err=True,
        )


def _has_disposal_capability(container: DisposableContainer) -> bool:
    try:
        result = _docker(
            "container",
            "inspect",
            container.container_id,
            timeout=_DOCKER_RM_TIMEOUT_SECONDS,
        )
    except click.ClickException as exc:
        _warn_cleanup_refusal(container, f"inspection failed: {exc}")
        return False
    if result.returncode != 0:
        _warn_cleanup_refusal(container, f"inspection failed: {_stderr_tail(result)}")
        return False
    try:
        payload: object = json.loads(_process_output(result.stdout))
    except json.JSONDecodeError as exc:
        _warn_cleanup_refusal(container, f"inspection returned invalid JSON: {exc}")
        return False
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        _warn_cleanup_refusal(container, "inspection returned an unexpected record")
        return False

    record = payload[0]
    config = record.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict) or not all(isinstance(key, str) for key in labels):
        _warn_cleanup_refusal(container, "inspection returned invalid labels")
        return False
    has_compose_labels = any(key.startswith(_COMPOSE_LABEL_PREFIX) for key in labels)
    authorized = (
        record.get("Id") == container.container_id
        and record.get("Name") == f"/{container.name}"
        and labels.get(_DISPOSABLE_LABEL) == "true"
        and labels.get(_DISPOSABLE_RUN_LABEL) == container.run_id
        and not has_compose_labels
    )
    if not authorized:
        _warn_cleanup_refusal(container, "identity or disposal labels did not match")
    return authorized


def _warn_cleanup_refusal(container: DisposableContainer, reason: str) -> None:
    click.echo(
        f"Warning: refusing to remove scratch container {container.name}: {reason}",
        err=True,
    )


def _require_file(path: Path, label: str) -> None:
    require_regular_file(path, label=f"{label} for restore verification")


def _stderr_tail(result: subprocess.CompletedProcess[bytes], *, limit: int = 800) -> str:
    detail = _process_output(result.stderr) or _process_output(result.stdout)
    return detail[-limit:] or f"exit {result.returncode}"


def _verified(method: str) -> VerificationState:
    return VerificationState(
        verified=True,
        method=method,
        timestamp=datetime.now(UTC).isoformat(),
    )
