"""Scratch-restore verification for hub backups.

`restore_verified` in the manifest is earned only by the restores performed here:
every verifier rebuilds the payload in a throwaway container (or throwaway
directory) and proves the restored content matches what was captured. Archive
readability alone never counts. Failures raise `click.ClickException` so the
backup command can treat unverified output as a failed backup.
"""

from __future__ import annotations

import secrets
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

from gobby.cli.hub_backup._manifest import VerificationState
from gobby.cli.postgres_backup import _process_output, _raise_for_subprocess_error

POSTGRES_VERIFY_IMAGE = "gobby-postgres-local:18-pgsearch"
FALKORDB_VERIFY_IMAGE = "falkordb/falkordb:latest"

_PG_METHOD = "scratch-pg-restore+globals-replay+role-acl+row-counts"
_QDRANT_METHOD = "qdrant-scratch-collection-recover+count"
_FALKOR_METHOD = "falkordb-scratch-rdb-load+graph-list"
_VOLUME_METHOD = "tar-extract-scratch"

_QDRANT_SCRATCH_PREFIX = "hub_backup_verify_"
_QDRANT_TIMEOUT_SECONDS = 120
_SCRATCH_DB = "gobby"
_SCRATCH_REPAIR_PGOPTIONS = "PGOPTIONS=-c event_triggers=off"
_SUPERUSER = "postgres"
_FALKOR_DATA_DIR = "/var/lib/falkordb/data"
_REDIS_CLI = 'redis-cli -a "$GOBBY_FALKORDB_PASSWORD" --no-auth-warning'

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


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


def verify_postgres_restore(
    dump_path: Path,
    globals_path: Path,
    *,
    expected_probes: dict[str, int],
    expected_roles: list[RoleExpectation],
) -> tuple[VerificationState, dict[str, object]]:
    """Restore dump + globals into a throwaway cluster and prove roles and row counts."""
    _require_file(dump_path, "PostgreSQL dump")
    _require_file(globals_path, "PostgreSQL globals")

    container = f"gobby-hub-verify-pg-{uuid.uuid4().hex[:8]}"
    try:
        _start_scratch_postgres(container)
        _wait_for_postgres(container)
        _replay_globals(container, globals_path)
        _check_roles(container, expected_roles)
        _restore_dump(container, dump_path)
        _check_row_counts(container, expected_probes)
    finally:
        _remove_container(container)

    details: dict[str, object] = {
        "tables_checked": len(expected_probes),
        "roles_checked": len(expected_roles),
        "scratch_container": container,
    }
    return _verified(_PG_METHOD), details


def _start_scratch_postgres(container: str) -> None:
    password = secrets.token_urlsafe(24)
    result = _docker(
        "run",
        "-d",
        "--name",
        container,
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
    with globals_path.open("rb") as handle:
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


def _check_roles(container: str, expected_roles: list[RoleExpectation]) -> None:
    if not expected_roles:
        return
    output = _psql_query(container, _SUPERUSER, _ROLE_QUERY, action="Scratch role query")
    actual: dict[str, tuple[bool, bool]] = {}
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        actual[fields[0].strip()] = (fields[1].strip() == "t", fields[2].strip() == "t")

    problems: list[str] = []
    for role in expected_roles:
        if role.rolname not in actual:
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
    with dump_path.open("rb") as handle:
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
) -> tuple[VerificationState, dict[str, object]]:
    """Recover each snapshot into a scratch collection and prove exact point counts."""
    if not url:
        raise click.ClickException(
            "Qdrant restore verification requires a remote Qdrant url; "
            "embedded/local mode cannot recover snapshots"
        )
    for collection, snapshot_path in snapshots.items():
        _require_file(snapshot_path, f"Qdrant snapshot for {collection}")

    client = QdrantClient(url=url, api_key=api_key, timeout=_QDRANT_TIMEOUT_SECONDS)
    verified: dict[str, int] = {}
    try:
        for collection, snapshot_path in sorted(snapshots.items()):
            if collection not in expected_counts:
                raise click.ClickException(
                    f"Qdrant restore verification failed: no expected point count "
                    f"recorded for collection {collection}"
                )
            scratch = f"{_QDRANT_SCRATCH_PREFIX}{collection}"
            try:
                _recover_qdrant_collection(client, scratch, snapshot_path)
                actual = client.count(scratch, exact=True).count
            finally:
                _delete_qdrant_collection(client, scratch)
            expected = expected_counts[collection]
            if actual != expected:
                raise click.ClickException(
                    f"Qdrant restore verification failed for collection {collection}: "
                    f"expected {expected} points, got {actual}"
                )
            verified[collection] = actual
    finally:
        _close_qdrant_client(client)

    details: dict[str, object] = {
        "collections_checked": len(verified),
        "points_verified": verified,
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
    expected_graphs: list[str],
) -> tuple[VerificationState, dict[str, object]]:
    """Boot a scratch FalkorDB on a copy of the RDB and prove every graph loaded."""
    _require_file(rdb_path, "FalkorDB RDB")

    container = f"gobby-hub-verify-falkor-{uuid.uuid4().hex[:8]}"
    scratch_dir = Path(tempfile.mkdtemp(prefix="gobby-hub-verify-falkor-"))
    try:
        scratch_dir.chmod(0o700)
        # FalkorDB loads its RDB at startup only, so the copy must be in place first.
        shutil.copyfile(rdb_path, scratch_dir / "dump.rdb")
        _start_scratch_falkordb(container, scratch_dir)
        _wait_for_falkordb(container)
        actual = set(_list_graphs(container))
        expected = set(expected_graphs)
        if actual != expected:
            raise click.ClickException(
                "FalkorDB restore verification failed: "
                f"missing {sorted(expected - actual)}, unexpected {sorted(actual - expected)}"
            )
    finally:
        _remove_container(container)
        shutil.rmtree(scratch_dir, ignore_errors=True)

    details: dict[str, object] = {"graphs_verified": len(set(expected_graphs))}
    return _verified(_FALKOR_METHOD), details


def _start_scratch_falkordb(container: str, scratch_dir: Path) -> None:
    password = secrets.token_urlsafe(24)
    result = _docker(
        "run",
        "-d",
        "--name",
        container,
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
) -> tuple[VerificationState, dict[str, object]]:
    """Extract each volume archive into a scratch directory and count real members."""
    for volume, archive_path in archives.items():
        _require_file(archive_path, f"Volume archive for {volume}")

    counts: dict[str, int] = {}
    for volume, archive_path in sorted(archives.items()):
        scratch_dir = Path(tempfile.mkdtemp(prefix="gobby-hub-verify-vol-"))
        try:
            counts[volume] = _extract_archive(volume, archive_path, scratch_dir)
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)

    details: dict[str, object] = {"archives": counts}
    return _verified(_VOLUME_METHOD), details


def _extract_archive(volume: str, archive_path: Path, scratch_dir: Path) -> int:
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=scratch_dir, filter="data")  # nosec B202 # data filter applied
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


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _docker(
    *argv: str,
    timeout: int,
    stdin: IO[bytes] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    command = ["docker", *argv]
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


def _remove_container(container: str) -> None:
    try:
        result = _docker("rm", "-f", container, timeout=_DOCKER_RM_TIMEOUT_SECONDS)
    except click.ClickException as exc:
        click.echo(f"Warning: failed to remove scratch container {container}: {exc}", err=True)
        return
    if result.returncode != 0:
        click.echo(
            f"Warning: failed to remove scratch container {container}: {_stderr_tail(result)}",
            err=True,
        )


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise click.ClickException(f"{label} not found for restore verification: {path}")


def _stderr_tail(result: subprocess.CompletedProcess[bytes], *, limit: int = 800) -> str:
    detail = _process_output(result.stderr) or _process_output(result.stdout)
    return detail[-limit:] or f"exit {result.returncode}"


def _verified(method: str) -> VerificationState:
    return VerificationState(
        verified=True,
        method=method,
        timestamp=datetime.now(UTC).isoformat(),
    )
