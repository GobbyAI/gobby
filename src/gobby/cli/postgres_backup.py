"""Logical PostgreSQL backup and restore helpers."""

from __future__ import annotations

import hashlib
import json
import subprocess  # nosec B404 # fixed pg_dump/pg_restore/docker commands
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

import click
import psycopg

from gobby import __version__
from gobby.cli import postgres_bootstrap as _bootstrap
from gobby.cli.installers.postgres import (
    DEFAULT_POSTGRES_DB,
    DEFAULT_POSTGRES_USER,
    _active_install_mode,
    _extension_present,
    _migration_complete,
    _read_bootstrap_database_url,
)
from gobby.cli.postgres_bootstrap import InstallMode
from gobby.cli.utils import get_gobby_home
from gobby.config.bootstrap import BootstrapConfigError

POSTGRES_DUMP_NAME = "gobby.dump"
POSTGRES_METADATA_NAME = "metadata.json"
POSTGRES_SHA256SUMS_NAME = "SHA256SUMS"
POSTGRES_BACKUP_ARCHIVE_PREFIX = "gobby/postgres"

_POSTGRES_CONTAINER = "gobby-postgres"
_SUBPROCESS_TIMEOUT_SECONDS = 600


def postgres_backup_configured(*, gobby_home: Path | None = None) -> bool:
    """Return true when bootstrap state has a PostgreSQL backup target."""
    home = gobby_home or get_gobby_home()
    try:
        data = _bootstrap.read_bootstrap_yaml(_bootstrap.bootstrap_path(home))
    except Exception:
        return False
    return _has_text(data.get("database_url")) or _has_text(data.get("database_url_ref"))


def create_postgres_backup(
    *,
    output_dir: Path | None = None,
    gobby_home: Path | None = None,
) -> dict[str, Any]:
    """Create and verify a custom-format logical PostgreSQL backup."""
    home = gobby_home or get_gobby_home()
    backup_dir = output_dir.expanduser() if output_dir else _default_backup_dir(home)
    if backup_dir.exists() and any(backup_dir.iterdir()):
        raise click.ClickException(f"Backup directory is not empty: {backup_dir}")
    backup_dir.mkdir(parents=True, exist_ok=True)

    mode = _active_install_mode(gobby_home=home)
    database_url = _resolve_database_url(home)
    dump_path = backup_dir / POSTGRES_DUMP_NAME

    metadata = _collect_source_metadata(database_url=database_url, mode=mode)
    _run_pg_dump(mode=mode, database_url=database_url, dump_path=dump_path)
    _verify_dump_with_pg_restore(mode=mode, dump_path=dump_path)

    dump_sha256 = _sha256_file(dump_path)
    metadata["dump_sha256"] = dump_sha256
    metadata_path = backup_dir / POSTGRES_METADATA_NAME
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sha_path = backup_dir / POSTGRES_SHA256SUMS_NAME
    _write_sha256s(backup_dir, sha_path)
    _verify_sha256s(backup_dir, required_dump_sha256=dump_sha256)

    return {
        "backup_dir": str(backup_dir),
        "dump_path": str(dump_path),
        "metadata_path": str(metadata_path),
        "sha256s_path": str(sha_path),
        "dump_sha256": dump_sha256,
        "database_url": _redact_dsn(database_url),
        "mode": mode,
        "verified": True,
    }


def restore_postgres_backup(
    source: Path,
    *,
    clean: bool = False,
    gobby_home: Path | None = None,
) -> dict[str, Any]:
    """Verify and restore a PostgreSQL backup file or backup directory."""
    home = gobby_home or get_gobby_home()
    mode = _active_install_mode(gobby_home=home)
    database_url = _resolve_database_url(home)
    dump_path = _resolve_dump_path(source.expanduser())

    metadata = _read_metadata_for_dump(dump_path)
    expected_sha256 = _expected_dump_sha256(dump_path, metadata)
    if expected_sha256:
        actual_sha256 = _sha256_file(dump_path)
        if actual_sha256 != expected_sha256:
            raise click.ClickException(
                "PostgreSQL dump checksum mismatch: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
    _verify_dump_with_pg_restore(mode=mode, dump_path=dump_path)
    _run_pg_restore(mode=mode, database_url=database_url, dump_path=dump_path, clean=clean)
    probes = _run_post_restore_probes(
        database_url=database_url,
        mode=mode,
        gobby_home=home,
    )

    return {
        "source": str(source),
        "dump_path": str(dump_path),
        "database_url": _redact_dsn(database_url),
        "mode": mode,
        "clean": clean,
        "verified": True,
        "probes": probes,
    }


def backup_payload_paths(backup_dir: Path) -> list[tuple[str, Path]]:
    """Return archive paths for a backup directory created by create_postgres_backup."""
    return [
        (f"{POSTGRES_BACKUP_ARCHIVE_PREFIX}/{POSTGRES_DUMP_NAME}", backup_dir / POSTGRES_DUMP_NAME),
        (
            f"{POSTGRES_BACKUP_ARCHIVE_PREFIX}/{POSTGRES_METADATA_NAME}",
            backup_dir / POSTGRES_METADATA_NAME,
        ),
        (
            f"{POSTGRES_BACKUP_ARCHIVE_PREFIX}/{POSTGRES_SHA256SUMS_NAME}",
            backup_dir / POSTGRES_SHA256SUMS_NAME,
        ),
    ]


def _default_backup_dir(gobby_home: Path) -> Path:
    return gobby_home / "backups" / "postgres" / _utc_timestamp()


def _collect_source_metadata(*, database_url: str, mode: InstallMode) -> dict[str, Any]:
    try:
        with psycopg.connect(database_url, connect_timeout=10) as conn:
            version_row = conn.execute("SHOW server_version").fetchone()
            migration_marker = _migration_complete(conn)
            return {
                "format_version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "gobby_version": __version__,
                "source_postgres_version": str(version_row[0]) if version_row else "unknown",
                "source_dsn_redacted": _redact_dsn(database_url),
                "install_mode": mode,
                "database_name": _dsn_db(database_url),
                "pg_search_present": _extension_present(conn, "pg_search"),
                "pgaudit_present": _extension_present(conn, "pgaudit"),
                "migration_marker": migration_marker,
                "dump_file": POSTGRES_DUMP_NAME,
                "dump_format": "custom",
            }
    except psycopg.Error as exc:
        raise click.ClickException(f"Unable to inspect PostgreSQL before backup: {exc}") from exc


def _run_pg_dump(*, mode: InstallMode, database_url: str, dump_path: Path) -> None:
    if mode == "docker":
        user = _dsn_user(database_url) or DEFAULT_POSTGRES_USER
        database = _dsn_db(database_url) or DEFAULT_POSTGRES_DB
        command = [
            "docker",
            "exec",
            _POSTGRES_CONTAINER,
            "pg_dump",
            "-U",
            user,
            "-d",
            database,
            "-Fc",
            "--no-owner",
            "--no-privileges",
        ]
        try:
            with dump_path.open("wb") as output:
                result = subprocess.run(  # nosec B603 B607
                    command,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    timeout=_SUBPROCESS_TIMEOUT_SECONDS,
                )
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
            raise click.ClickException(f"Docker pg_dump failed: {exc}") from exc
        _raise_for_subprocess_error(result, "Docker pg_dump")
        return

    command = [
        "pg_dump",
        "-Fc",
        "--no-owner",
        "--no-privileges",
        "--dbname",
        database_url,
        "--file",
        str(dump_path),
    ]
    _run_checked(command, "pg_dump")


def _run_pg_restore(
    *,
    mode: InstallMode,
    database_url: str,
    dump_path: Path,
    clean: bool,
) -> None:
    options = ["--no-owner", "--no-privileges"]
    if clean:
        options.extend(["--clean", "--if-exists"])

    if mode == "docker":
        user = _dsn_user(database_url) or DEFAULT_POSTGRES_USER
        database = _dsn_db(database_url) or DEFAULT_POSTGRES_DB
        command = [
            "docker",
            "exec",
            "-i",
            _POSTGRES_CONTAINER,
            "pg_restore",
            *options,
            "-U",
            user,
            "-d",
            database,
        ]
        try:
            with dump_path.open("rb") as stdin:
                result = subprocess.run(  # nosec B603 B607
                    command,
                    stdin=stdin,
                    capture_output=True,
                    timeout=_SUBPROCESS_TIMEOUT_SECONDS,
                )
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
            raise click.ClickException(f"Docker pg_restore failed: {exc}") from exc
        _raise_for_subprocess_error(result, "Docker pg_restore")
        return

    command = ["pg_restore", *options, "--dbname", database_url, str(dump_path)]
    _run_checked(command, "pg_restore")


def _verify_dump_with_pg_restore(*, mode: InstallMode, dump_path: Path) -> None:
    if not dump_path.is_file():
        raise click.ClickException(f"PostgreSQL dump was not created: {dump_path}")
    if mode == "docker":
        command = ["docker", "exec", "-i", _POSTGRES_CONTAINER, "pg_restore", "--list"]
        try:
            with dump_path.open("rb") as stdin:
                result = subprocess.run(  # nosec B603 B607
                    command,
                    stdin=stdin,
                    capture_output=True,
                    timeout=120,
                )
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
            raise click.ClickException(f"Docker pg_restore --list failed: {exc}") from exc
        _raise_for_subprocess_error(result, "Docker pg_restore --list")
        return

    _run_checked(["pg_restore", "--list", str(dump_path)], "pg_restore --list", timeout=120)


def _run_post_restore_probes(
    *,
    database_url: str,
    mode: InstallMode,
    gobby_home: Path,
) -> dict[str, Any]:
    try:
        resolved_bootstrap_url = _resolve_database_url(gobby_home)
        with psycopg.connect(database_url, connect_timeout=10) as conn:
            pg_search_present = _extension_present(conn, "pg_search")
            pgaudit_present = _extension_present(conn, "pgaudit")
            migration_marker = _migration_complete(conn)
            probes: dict[str, Any] = {
                "connectivity": True,
                "pg_search_present": pg_search_present,
                "pgaudit_present": pgaudit_present,
                "migration_marker": migration_marker,
                "bootstrap_resolved": bool(resolved_bootstrap_url),
                "target_dsn_redacted": _redact_dsn(resolved_bootstrap_url),
            }
            if not pg_search_present:
                raise click.ClickException("PostgreSQL restore probe failed: pg_search missing")
            if mode == "docker" and not pgaudit_present:
                raise click.ClickException("PostgreSQL restore probe failed: pgaudit missing")
            if not migration_marker.get("present"):
                raise click.ClickException(
                    "PostgreSQL restore probe failed: migration marker missing"
                )
            return probes
    except psycopg.Error as exc:
        raise click.ClickException(f"PostgreSQL restore probe failed: {exc}") from exc


def _resolve_database_url(gobby_home: Path) -> str:
    try:
        database_url = _read_bootstrap_database_url(gobby_home)
    except BootstrapConfigError as exc:
        raise click.ClickException(f"Unable to resolve PostgreSQL bootstrap DSN: {exc}") from exc
    if not database_url:
        raise click.ClickException(
            "PostgreSQL bootstrap DSN is not configured. Run `gobby postgres install` first."
        )
    return database_url


def _resolve_dump_path(source: Path) -> Path:
    if source.is_dir():
        dump_path = source / POSTGRES_DUMP_NAME
    else:
        dump_path = source
    if not dump_path.is_file():
        raise click.ClickException(f"PostgreSQL dump not found: {dump_path}")
    return dump_path


def _read_metadata_for_dump(dump_path: Path) -> dict[str, Any] | None:
    metadata_path = dump_path.parent / POSTGRES_METADATA_NAME
    if not metadata_path.is_file():
        return None
    data = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise click.ClickException(f"Invalid PostgreSQL backup metadata: {metadata_path}")
    return cast(dict[str, Any], data)


def _expected_dump_sha256(dump_path: Path, metadata: dict[str, Any] | None) -> str | None:
    metadata_sha256 = None
    if metadata:
        checksum = metadata.get("dump_sha256")
        if isinstance(checksum, str) and checksum:
            metadata_sha256 = checksum
    sums_sha256 = _sha256_from_sums(dump_path.parent / POSTGRES_SHA256SUMS_NAME, POSTGRES_DUMP_NAME)
    if metadata_sha256 and sums_sha256 and metadata_sha256 != sums_sha256:
        raise click.ClickException("PostgreSQL backup metadata and SHA256SUMS disagree")
    return metadata_sha256 or sums_sha256


def _sha256_from_sums(path: Path, filename: str) -> str | None:
    return _read_sha256s(path).get(filename)


def _write_sha256s(backup_dir: Path, output_path: Path) -> None:
    lines = [
        f"{_sha256_file(backup_dir / POSTGRES_DUMP_NAME)}  {POSTGRES_DUMP_NAME}",
        f"{_sha256_file(backup_dir / POSTGRES_METADATA_NAME)}  {POSTGRES_METADATA_NAME}",
    ]
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _verify_sha256s(backup_dir: Path, *, required_dump_sha256: str) -> None:
    sums_path = backup_dir / POSTGRES_SHA256SUMS_NAME
    entries = _read_sha256s(sums_path)
    recorded_dump_sha256 = entries.get(POSTGRES_DUMP_NAME)
    if recorded_dump_sha256 != required_dump_sha256:
        raise click.ClickException("PostgreSQL backup SHA256SUMS does not match dump checksum")
    for filename, expected_sha256 in entries.items():
        file_path = backup_dir / filename
        if not file_path.is_file():
            raise click.ClickException(
                f"PostgreSQL backup SHA256SUMS references missing {filename}"
            )
        actual_sha256 = _sha256_file(file_path)
        if actual_sha256 != expected_sha256:
            raise click.ClickException(
                f"PostgreSQL backup SHA256SUMS mismatch for {filename}: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )


def _read_sha256s(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        checksum, separator, name = line.partition("  ")
        if separator:
            entries[name] = checksum
    return entries


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_checked(
    command: list[str],
    action: str,
    *,
    timeout: int = _SUBPROCESS_TIMEOUT_SECONDS,
) -> None:
    try:
        result = subprocess.run(  # nosec B603 # fixed executable names and argument vectors
            command,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(f"{action} failed: command not found") from exc
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        raise click.ClickException(f"{action} failed: {exc}") from exc
    _raise_for_subprocess_error(result, action)


def _raise_for_subprocess_error(result: subprocess.CompletedProcess[Any], action: str) -> None:
    if result.returncode == 0:
        return
    detail = _process_output(result.stderr) or _process_output(result.stdout)
    raise click.ClickException(f"{action} failed: {detail or f'exit {result.returncode}'}")


def _process_output(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    if isinstance(value, str):
        return value.strip()
    return ""


def _redact_dsn(dsn: str) -> str:
    if "@" not in dsn:
        return dsn
    prefix, suffix = dsn.split("@", 1)
    scheme, auth = prefix.split("://", 1) if "://" in prefix else ("", prefix)
    if ":" not in auth:
        return dsn
    user = auth.split(":", 1)[0]
    redacted_auth = f"{user}:****"
    if scheme:
        return f"{scheme}://{redacted_auth}@{suffix}"
    return f"{redacted_auth}@{suffix}"


def _dsn_user(dsn: str) -> str | None:
    parsed = urlparse(dsn)
    return unquote(parsed.username) if parsed.username else None


def _dsn_db(dsn: str) -> str | None:
    parsed = urlparse(dsn)
    return unquote(parsed.path.lstrip("/")) if parsed.path else None


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
