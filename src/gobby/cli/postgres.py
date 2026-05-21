"""CLI commands for PostgreSQL hub database management."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import shlex
import shutil
import subprocess  # nosec B404 # subprocess needed for Docker pgAudit readback probes
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import click
import psycopg

from gobby import __version__
from gobby.cli.installers.postgres import (
    DEFAULT_POSTGRES_PORT,
    _active_install_mode,
    _docker_database_url,
    _extension_present,
    _external_ownership_status,
    _migration_complete,
    _preload_libraries,
    _read_bootstrap_database_url,
    get_postgres_status,
    install_postgres,
    render_postgres_status,
    uninstall_postgres,
)
from gobby.cli.installers.service import get_service_status
from gobby.cli.postgres_bootstrap import InstallMode, set_bootstrap_field
from gobby.cli.utils import _is_process_alive, get_gobby_home
from gobby.storage.migration.sqlite_to_postgres import (
    SqliteToPostgresMigrationError,
    migrate_sqlite_to_postgres,
)

_NO_ROLLBACK_ACK = "I accept no-rollback risk"
_CAPTURE_SINK_KINDS = {"pgaudit-file", "wal-archive"}
_TICKET_CAPTURE_KINDS = {"pgaudit-managed", "pgaudit-file", "wal-archive", "none"}
_PGAUDIT_CONTAINER = "gobby-postgres"
_PGAUDIT_LOG_DIR = "/var/log/pgaudit"
_WAL_ARCHIVE_SLOT_KEYS = ("slot_name", "slot", "replication_slot")


@click.group("postgres")
def postgres_cli() -> None:
    """Manage the local PostgreSQL hub database."""


@postgres_cli.command("install")
@click.option(
    "--mode",
    type=click.Choice(["docker", "native", "external"]),
    default="docker",
    show_default=True,
    help="Install mode. docker is recommended.",
)
@click.option(
    "--dsn",
    default=None,
    help="psycopg DSN. Required for --mode external; optional for --mode native.",
)
def install_cmd(mode: str, dsn: str | None) -> None:
    """Install or configure PostgreSQL."""
    result = install_postgres(mode=_install_mode(mode), dsn=dsn)
    _render_install_result(result)


@postgres_cli.command("status")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help=("Emit the status payload as JSON on stdout. Default output is human-readable text."),
)
def status_cmd(as_json: bool) -> None:
    """Show PostgreSQL health, extension, migration, and ownership status."""
    payload = asyncio.run(get_postgres_status())
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        click.echo(render_postgres_status(payload))


@postgres_cli.command("uninstall")
@click.option(
    "--remove-data",
    is_flag=True,
    default=False,
    help=(
        "Docker mode: also delete the gobby_postgres_data and gobby_pgaudit_log "
        "named volumes. Native mode: print manual data-directory deletion steps. "
        "External mode: refuses; Gobby never deletes server-side data. "
        "Does not restore the removed SQLite hub runtime."
    ),
)
def uninstall_cmd(remove_data: bool) -> None:
    """Clean up PostgreSQL service artifacts using the recorded install mode."""
    gobby_home = get_gobby_home()
    result = uninstall_postgres(
        mode=_active_install_mode(gobby_home=gobby_home),
        gobby_home=gobby_home,
        remove_data=remove_data,
    )
    _render_uninstall_result(result)


@postgres_cli.command("migrate-from-sqlite")
@click.option(
    "--source",
    type=click.Path(path_type=Path),
    default=Path("~/.gobby/gobby-hub.db"),
    show_default=True,
    help="SQLite hub database to import.",
)
@click.option(
    "--target",
    required=True,
    help="Target PostgreSQL DSN.",
)
@click.option(
    "--batch-size",
    type=click.IntRange(min=1),
    default=1000,
    show_default=True,
    help="Rows to read from SQLite per table batch.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Run read-only preflight checks without changing the target.",
)
def migrate_from_sqlite(
    source: Path,
    target: str,
    batch_size: int,
    dry_run: bool,
) -> None:
    """Import the SQLite hub database into PostgreSQL."""
    if _daemon_running():
        raise click.ClickException("Stop the daemon first: gobby stop")

    try:
        result = migrate_sqlite_to_postgres(
            source=source.expanduser(),
            target=target,
            batch_size=batch_size,
            dry_run=dry_run,
        )
    except SqliteToPostgresMigrationError as exc:
        raise click.ClickException(str(exc)) from exc
    except (OSError, psycopg.Error) as exc:
        raise click.ClickException(str(exc)) from exc

    _render_migration_result(result)


@postgres_cli.command("activate")
@click.option(
    "--capture-sink",
    default=None,
    metavar="TYPE:LOCATION",
    help=(
        "Native/external mode only. Declares the operator-wired write-capture sink. "
        "TYPE must be exactly 'pgaudit-file' or 'wal-archive'. Mutually exclusive "
        "with --accept-no-rollback-risk."
    ),
)
@click.option(
    "--accept-no-rollback-risk",
    is_flag=True,
    default=False,
    help=(
        "Native/external mode only. Acknowledges that no validation-window writes will "
        "be auto-captured; rollback will rely on the pre-cutover SQLite backup. "
        "Requires typing the confirmation phrase. Mutually exclusive with --capture-sink."
    ),
)
def activate_cmd(capture_sink: str | None, accept_no_rollback_risk: bool) -> None:
    """Activate PostgreSQL as the hub database runtime backend."""
    if _daemon_running():
        raise click.ClickException("Stop the daemon first: gobby stop")
    if not _postgres_migration_complete():
        raise click.ClickException("Run `gobby postgres migrate-from-sqlite` first")

    mode = _active_install_mode(gobby_home=get_gobby_home())
    if mode == "external":
        _require_ownership_sentinel_or_fail()

    if mode == "docker":
        if capture_sink or accept_no_rollback_risk:
            raise click.ClickException(
                "Capture flags are not applicable in docker mode; pgAudit is the gate."
            )
        probe = _probe_pgaudit_or_fail()
        ticket = _build_cutover_ticket(
            mode=mode,
            capture_kind="pgaudit-managed",
            capture_value=None,
            verification=_verification_ok(probe),
        )
    else:
        ticket = _build_native_external_ticket(
            mode=mode,
            capture_sink=capture_sink,
            accept_no_rollback_risk=accept_no_rollback_risk,
        )

    backup_path = _backup_bootstrap()
    _set_bootstrap_field("hub_backend", "postgres")
    try:
        _write_cutover_ticket(ticket)
    except Exception:
        _restore_bootstrap(backup_path)
        raise

    click.echo("hub_backend set to postgres.")
    click.echo("PostgreSQL is now the required hub runtime.")
    click.echo("For validation-window recovery guidance:")
    click.echo("  docs/runbooks/postgres-rollback.md")
    click.echo(f"Cutover ticket: {ticket['_path']}")
    click.echo(f"Validation-window deadline: {ticket['deadline_at']}")


@postgres_cli.command("deactivate")
def deactivate_cmd() -> None:
    """Deprecated compatibility command for the removed SQLite hub runtime."""
    raise click.ClickException(
        "PostgreSQL is the only supported hub runtime. "
        "`gobby postgres deactivate` no longer writes hub_backend=sqlite. "
        "hub_backend=sqlite cannot start under the Phase 7 runtime. "
        "Use `gobby postgres migrate-from-sqlite` only for legacy imports, "
        "and follow docs/runbooks/postgres-rollback.md for recovery exports."
    )


def _render_install_result(result: dict[str, Any]) -> None:
    if result.get("success"):
        click.echo(result.get("message", "PostgreSQL configured"))
        if result.get("mode"):
            click.echo(f"  Mode: {result['mode']}")
        if result.get("database_url"):
            click.echo(f"  DSN:  {_redact_dsn(str(result['database_url']))}")
        if result.get("compose_file"):
            click.echo(f"  Compose: {result['compose_file']}")
        pgaudit_available = result.get("pgaudit_available")
        if pgaudit_available is not None:
            click.echo(f"  pgaudit available: {'yes' if pgaudit_available else 'no'}")
        click.echo("\nRestart the daemon after cutover when hub_backend is activated.")
        return

    click.echo(f"Failed: {result.get('error', 'unknown error')}", err=True)
    sys.exit(1)


def _render_uninstall_result(result: dict[str, Any]) -> None:
    if result.get("success"):
        click.echo(result.get("message", "PostgreSQL service cleanup completed"))
        if result.get("data_removed"):
            click.echo("  Docker data volumes removed")
        for step in result.get("manual_steps", []):
            click.echo(f"  {step}")
        return

    click.echo(f"Failed: {result.get('error', 'unknown error')}", err=True)
    sys.exit(1)


def _render_migration_result(result: dict[str, Any]) -> None:
    rows = int(result.get("rows", 0))
    tables = int(result.get("tables", 0))
    if result.get("dry_run"):
        click.echo(f"dry-run: would import {rows} rows across {tables} tables")
    else:
        click.echo(f"imported {rows} rows across {tables} tables")

    log_path = result.get("log_path")
    if log_path:
        click.echo(f"Import log: {log_path}")
    artifact_path = result.get("validation_artifact")
    if artifact_path:
        click.echo(f"Validation artifact: {artifact_path}")


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


def _install_mode(value: str) -> InstallMode:
    if value in {"docker", "native", "external"}:
        return cast(InstallMode, value)
    raise click.ClickException(f"Unknown install mode: {value}")


def _build_native_external_ticket(
    *,
    mode: InstallMode,
    capture_sink: str | None,
    accept_no_rollback_risk: bool,
) -> dict[str, Any]:
    if bool(capture_sink) == bool(accept_no_rollback_risk):
        raise click.ClickException(
            "Native/external mode requires exactly one of "
            "--capture-sink or --accept-no-rollback-risk."
        )

    if capture_sink:
        kind, location = _parse_capture_sink(capture_sink)
        probe = _probe_capture_sink_or_fail(kind, location)
        return _build_cutover_ticket(
            mode=mode,
            capture_kind=kind,
            capture_value=probe["capture_value"],
            verification=_verification_ok(probe),
        )

    acknowledgement = _require_typed_acknowledgement(_NO_ROLLBACK_ACK)
    return _build_cutover_ticket(
        mode=mode,
        capture_kind="none",
        capture_value=None,
        verification={
            "state": "operator-attested",
            "probed_at": None,
            "probe_detail": None,
        },
        acknowledgement=acknowledgement,
    )


def _daemon_running() -> bool:
    service_status = get_service_status()
    if service_status.get("running"):
        return True

    pid_file = get_gobby_home() / "gobby.pid"
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return False
    return _is_process_alive(pid)


def _postgres_migration_complete() -> bool:
    with _postgres_connection() as conn:
        return bool(_migration_complete(conn).get("present"))


def _require_ownership_sentinel_or_fail() -> None:
    with _postgres_connection() as conn:
        if not _external_ownership_status(conn).get("sentinel_present"):
            raise click.ClickException(
                "External PostgreSQL install ownership sentinel is missing. "
                "Run `gobby postgres install --mode external --dsn ...` first."
            )


def _probe_pgaudit_or_fail() -> dict[str, Any]:
    probe_token = f"gobby-pgaudit-probe-{uuid.uuid4().hex}"
    with _postgres_connection() as conn:
        if not _extension_present(conn, "pgaudit"):
            raise click.ClickException("pgAudit extension is not installed in PostgreSQL.")

        preload_libraries = _preload_libraries(conn)
        if "pgaudit" not in preload_libraries:
            raise click.ClickException("pgAudit is not loaded in shared_preload_libraries.")

        row = conn.execute("SELECT setting FROM pg_settings WHERE name = 'pgaudit.log'").fetchone()
        pgaudit_log = str(row[0]) if row else ""
        log_tokens = {token.strip() for token in pgaudit_log.split(",") if token.strip()}
        if not ({"write", "all"} & log_tokens):
            raise click.ClickException("pgAudit must be configured with pgaudit.log=write.")

        try:
            row = conn.execute(
                f"/* {probe_token} */ "
                "UPDATE _pgaudit_probe SET last_probed_at = NOW() WHERE id = 1 "
                "RETURNING last_probed_at"
            ).fetchone()
            conn.commit()
        except psycopg.Error as exc:
            raise click.ClickException(f"pgAudit write probe failed: {exc}") from exc
        if row is None:
            raise click.ClickException("pgAudit probe table _pgaudit_probe is missing seed row.")

    log_probe = _probe_docker_pgaudit_log_or_fail(probe_token)

    return {
        "extension": "pgaudit",
        "shared_preload_libraries": preload_libraries,
        "pgaudit_log": pgaudit_log,
        "write_probe": "ok",
        "audit_file": log_probe["audit_file"],
        "audit_readback": log_probe["audit_readback"],
    }


def _probe_capture_sink_or_fail(kind: str, location: str) -> dict[str, Any]:
    if kind == "pgaudit-file":
        path = Path(location).expanduser()
        if not path.is_absolute():
            raise click.ClickException("pgaudit-file capture sink must be an absolute path.")
        if not path.exists():
            raise click.ClickException("pgaudit-file capture sink must already exist.")
        if path.is_dir():
            raise click.ClickException("pgaudit-file capture sink must be a file path.")
        with path.open("a", encoding="utf-8") as handle:
            handle.write("")
        return {
            "kind": kind,
            "capture_value": str(path),
            "writable": True,
        }

    if kind == "wal-archive":
        slot_name = _wal_archive_slot_name(location)
        try:
            with _postgres_connection() as conn:
                row = conn.execute(
                    "SELECT slot_name FROM pg_replication_slots WHERE slot_name = %s",
                    (slot_name,),
                ).fetchone()
        except psycopg.Error as exc:
            raise click.ClickException(
                f"Unable to verify wal-archive replication slot {slot_name!r}: {exc}"
            ) from exc
        if row is None:
            raise click.ClickException(
                f"wal-archive capture sink replication slot {slot_name!r} was not found."
            )
        return {
            "kind": kind,
            "capture_value": location,
            "replication_slot": slot_name,
        }

    raise click.ClickException(f"Unknown capture-sink type {kind!r}.")


def _probe_docker_pgaudit_log_or_fail(probe_token: str) -> dict[str, str]:
    token_arg = shlex.quote(probe_token)
    script = f"""
set -eu
test -d {_PGAUDIT_LOG_DIR}
audit_file="$(find {_PGAUDIT_LOG_DIR} -name 'pgaudit-*.log' -size +0c -type f | sort | tail -n1)"
test -n "$audit_file"
test "$(stat -c '%U %a' "$audit_file")" = "postgres 640"
audit_line="$(grep -E 'LOG:  AUDIT: SESSION,.*UPDATE' "$audit_file" | grep -F {token_arg} | tail -n1)"
test -n "$audit_line"
printf '%s\\n%s\\n' "$audit_file" "$audit_line"
""".strip()
    try:
        result = subprocess.run(  # nosec B603 B607 # fixed Docker exec command
            ["docker", "exec", _PGAUDIT_CONTAINER, "sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        raise click.ClickException(f"Unable to read pgAudit Docker log: {exc}") from exc

    if result.returncode != 0:
        detail = (
            result.stderr or result.stdout or f"docker exec exited {result.returncode}"
        ).strip()
        raise click.ClickException(f"pgAudit log readback probe failed: {detail}")

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise click.ClickException("pgAudit log readback probe did not return an AUDIT line.")
    return {"audit_file": lines[0], "audit_readback": lines[1]}


def _wal_archive_slot_name(location: str) -> str:
    value = location.strip()
    if not value:
        raise click.ClickException("wal-archive capture sink requires a location.")

    parsed = urlparse(value)
    if parsed.scheme:
        query = parse_qs(parsed.query)
        for key in _WAL_ARCHIVE_SLOT_KEYS:
            slot_values = query.get(key)
            if slot_values and slot_values[0].strip():
                return slot_values[0].strip()
        raise click.ClickException(
            "wal-archive capture sink DSN must include slot_name, slot, or replication_slot."
        )

    if "=" in value:
        for token in value.replace(";", " ").split():
            key, separator, raw_slot = token.partition("=")
            if separator and key in _WAL_ARCHIVE_SLOT_KEYS and raw_slot.strip():
                return raw_slot.strip()
        raise click.ClickException(
            "wal-archive capture sink spec must include slot_name, slot, or replication_slot."
        )

    return value


def _parse_capture_sink(capture_sink: str) -> tuple[str, str]:
    kind, separator, location = capture_sink.partition(":")
    if kind not in _CAPTURE_SINK_KINDS:
        raise click.ClickException(
            f"Unknown capture-sink type {kind!r}. Expected pgaudit-file or wal-archive."
        )
    if not separator or not location:
        raise click.ClickException(f"{kind} capture sink requires a location.")
    return kind, location


def _postgres_connection() -> Any:
    database_url = _read_bootstrap_database_url(get_gobby_home()) or _docker_database_url(
        DEFAULT_POSTGRES_PORT
    )
    try:
        return psycopg.connect(database_url, connect_timeout=5)
    except psycopg.Error as exc:
        raise click.ClickException(f"Unable to connect to PostgreSQL: {exc}") from exc


def _verification_ok(probe_detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": "ok",
        "probed_at": _utc_timestamp(),
        "probe_detail": probe_detail,
    }


def _require_typed_acknowledgement(phrase: str) -> dict[str, str]:
    typed = str(
        click.prompt(
            f'Type "{phrase}" to continue',
            default="",
            show_default=False,
            type=str,
        )
    )
    if typed != phrase:
        raise click.ClickException("Confirmation phrase did not match; activation aborted.")
    return {
        "phrase": phrase,
        "operator": getpass.getuser(),
        "asked_at": _utc_timestamp(),
    }


def _build_cutover_ticket(
    *,
    mode: InstallMode,
    capture_kind: str,
    capture_value: str | None,
    verification: dict[str, Any],
    acknowledgement: dict[str, str] | None = None,
) -> dict[str, Any]:
    if capture_kind not in _TICKET_CAPTURE_KINDS:
        raise click.ClickException(f"Unknown cutover capture kind: {capture_kind}")
    if capture_kind in _CAPTURE_SINK_KINDS and not capture_value:
        raise click.ClickException(f"{capture_kind} cutover ticket requires capture_value.")
    if capture_kind not in _CAPTURE_SINK_KINDS and capture_value is not None:
        raise click.ClickException(f"{capture_kind} cutover ticket must not set capture_value.")
    if capture_kind == "none" and acknowledgement is None:
        raise click.ClickException("No-rollback activation requires acknowledgement.")
    if capture_kind != "none" and acknowledgement is not None:
        raise click.ClickException(f"{capture_kind} cutover ticket must not set acknowledgement.")

    activated_at = datetime.now(UTC).replace(microsecond=0)
    ticket: dict[str, Any] = {
        "mode": mode,
        "activated_at": activated_at.isoformat(timespec="seconds"),
        "deadline_at": (activated_at + timedelta(hours=48)).isoformat(timespec="seconds"),
        "gobby_version": __version__,
        "capture_kind": capture_kind,
        "capture_value": capture_value,
        "verification": verification,
    }
    if acknowledgement is not None:
        ticket["acknowledgement"] = acknowledgement
    return ticket


def _backup_bootstrap() -> Path:
    bootstrap_path = _bootstrap_path()
    if not bootstrap_path.exists():
        raise click.ClickException("bootstrap.yaml not found; run `gobby install` first.")
    backup_path = bootstrap_path.with_name(
        f"bootstrap.yaml.{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.bak"
    )
    shutil.copy2(bootstrap_path, backup_path)
    return backup_path


def _restore_bootstrap(backup_path: Path) -> None:
    shutil.copy2(backup_path, _bootstrap_path())
    _bootstrap_path().chmod(0o600)


def _set_bootstrap_field(field: str, value: str) -> None:
    set_bootstrap_field(gobby_home=get_gobby_home(), field=field, value=value)


def _write_cutover_ticket(ticket: dict[str, Any]) -> None:
    migrations_dir = get_gobby_home() / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    path = migrations_dir / f"cutover-{_ticket_timestamp(ticket)}.json"
    tmp_path = path.with_suffix(".json.tmp")
    payload = {key: value for key, value in ticket.items() if key != "_path"}
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)
    ticket["_path"] = str(path)


def _ticket_timestamp(ticket: dict[str, Any]) -> str:
    value = str(ticket["activated_at"])
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _bootstrap_path() -> Path:
    return get_gobby_home() / "bootstrap.yaml"


def _utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat(timespec="seconds")
