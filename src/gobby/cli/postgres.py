"""CLI commands for PostgreSQL hub database management."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, cast

import click

from gobby.cli.installers.postgres import (
    InstallMode,
    _active_install_mode,
    get_postgres_status,
    install_postgres,
    render_postgres_status,
    uninstall_postgres,
)


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
    help=(
        "Rollback write-capture sink, such as pgaudit-file:/path or wal-archive:<dsn>. "
        "Mutually exclusive with --accept-no-rollback-risk."
    ),
)
@click.option(
    "--accept-no-rollback-risk",
    is_flag=True,
    default=False,
    help=(
        "Acknowledge that rollback cannot automatically capture validation-window writes. "
        "Mutually exclusive with --capture-sink."
    ),
)
def activate_cmd(capture_sink: str | None, accept_no_rollback_risk: bool) -> None:
    """Reserved for the cutover phase."""
    _ = (capture_sink, accept_no_rollback_risk)
    raise click.ClickException("PostgreSQL activation is implemented by the cutover phase.")


@postgres_cli.command("deactivate")
def deactivate_cmd() -> None:
    """Reserved for rollback after cutover."""
    raise click.ClickException("PostgreSQL deactivation is implemented by the cutover phase.")


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
