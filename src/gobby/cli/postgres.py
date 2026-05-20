"""CLI commands for PostgreSQL hub database management."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import click
import psycopg
import yaml

from gobby import __version__
from gobby.cli.installers.postgres import (
    DEFAULT_POSTGRES_PORT,
    InstallMode,
    _active_install_mode,
    _docker_database_url,
    _external_ownership_status,
    _migration_complete,
    _preload_libraries,
    _read_bootstrap_database_url,
    _read_bootstrap_yaml,
    get_postgres_status,
    install_postgres,
    render_postgres_status,
    uninstall_postgres,
)
from gobby.cli.installers.service import get_service_status
from gobby.cli.utils import _is_process_alive, get_gobby_home

_NO_ROLLBACK_ACK = "I accept no-rollback risk"
_CAPTURE_SINK_KINDS = {"pgaudit-file", "wal-archive"}
_TICKET_CAPTURE_KINDS = {"pgaudit-managed", "pgaudit-file", "wal-archive", "none"}


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
        "External mode: refuses; Gobby never deletes server-side data."
    ),
)
def uninstall_cmd(remove_data: bool) -> None:
    """Uninstall PostgreSQL using the recorded install mode."""
    result = uninstall_postgres(mode=_active_install_mode(), remove_data=remove_data)
    _render_uninstall_result(result)


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

    click.echo("hub_backend set to postgres. To roll back:")
    click.echo("  gobby stop && gobby postgres deactivate && gobby start")
    click.echo(f"Cutover ticket: {ticket['_path']}")
    click.echo(f"Validation-window deadline: {ticket['deadline_at']}")


@postgres_cli.command("deactivate")
def deactivate_cmd() -> None:
    """Deactivate PostgreSQL and return the hub runtime backend to SQLite."""
    if _daemon_running():
        raise click.ClickException("Stop the daemon first: gobby stop")
    backup_path = _backup_bootstrap()
    _set_bootstrap_field("hub_backend", "sqlite")
    click.echo("hub_backend set to sqlite.")
    click.echo(f"Bootstrap backup: {backup_path}")


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
        click.echo(result.get("message", "PostgreSQL uninstalled"))
        if result.get("data_removed"):
            click.echo("  Docker data volumes removed")
        for step in result.get("manual_steps", []):
            click.echo(f"  {step}")
        return

    click.echo(f"Failed: {result.get('error', 'unknown error')}", err=True)
    sys.exit(1)


def _redact_dsn(dsn: str) -> str:
    if "@" not in dsn or ":" not in dsn.split("@", 1)[0]:
        return dsn
    prefix, suffix = dsn.split("@", 1)
    scheme, auth = prefix.split("://", 1) if "://" in prefix else ("", prefix)
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
    with _postgres_connection() as conn:
        preload_libraries = _preload_libraries(conn)
        if "pgaudit" not in preload_libraries:
            raise click.ClickException("pgAudit is not loaded in shared_preload_libraries.")

        row = conn.execute("SELECT setting FROM pg_settings WHERE name = 'pgaudit.log'").fetchone()
        pgaudit_log = str(row[0]) if row else ""
        log_tokens = {token.strip() for token in pgaudit_log.split(",") if token.strip()}
        if not ({"write", "all"} & log_tokens):
            raise click.ClickException("pgAudit must be configured with pgaudit.log=write.")

        conn.execute("CREATE TEMP TABLE gobby_pgaudit_probe (id integer)")
        conn.execute("INSERT INTO gobby_pgaudit_probe (id) VALUES (1)")
        conn.execute("DROP TABLE gobby_pgaudit_probe")

    return {
        "extension": "pgaudit",
        "shared_preload_libraries": preload_libraries,
        "pgaudit_log": pgaudit_log,
        "write_probe": "ok",
    }


def _probe_capture_sink_or_fail(kind: str, location: str) -> dict[str, Any]:
    if kind == "pgaudit-file":
        path = Path(location).expanduser()
        if not path.is_absolute():
            raise click.ClickException("pgaudit-file capture sink must be an absolute path.")
        if path.exists():
            if path.is_dir():
                raise click.ClickException("pgaudit-file capture sink must be a file path.")
            with path.open("a", encoding="utf-8"):
                pass
        else:
            parent = path.parent
            if not parent.exists() or not parent.is_dir():
                raise click.ClickException(
                    "pgaudit-file capture sink parent directory does not exist."
                )
            _probe_directory_writable(parent)
        return {
            "kind": kind,
            "capture_value": str(path),
            "writable": True,
        }

    if kind == "wal-archive":
        if not location.strip():
            raise click.ClickException("wal-archive capture sink requires a location.")
        return {
            "kind": kind,
            "capture_value": location,
            "operator_declared": True,
        }

    raise click.ClickException(f"Unknown capture-sink type {kind!r}.")


def _probe_directory_writable(directory: Path) -> None:
    handle, probe_path = tempfile.mkstemp(
        prefix=".gobby-capture-probe-",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    os.close(handle)
    Path(probe_path).unlink(missing_ok=True)


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
    bootstrap_path = _bootstrap_path()
    data = _read_bootstrap_yaml(bootstrap_path)
    data[field] = value
    _write_bootstrap_yaml_atomic(bootstrap_path, data)


def _write_bootstrap_yaml_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            yaml.safe_dump(data, file, default_flow_style=False)
            file.flush()
            os.fsync(file.fileno())
        tmp_path.chmod(0o600)
        os.replace(tmp_path, path)
        path.chmod(0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


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
