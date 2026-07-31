"""CLI commands for PostgreSQL hub database management."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, cast

import click

from gobby.cli.installers.postgres import (
    _read_bootstrap_database_url,
    get_postgres_status,
    install_postgres,
    render_postgres_status,
)
from gobby.cli.installers.service import get_service_status
from gobby.cli.postgres_backup import create_postgres_backup, restore_postgres_backup
from gobby.cli.utils import _is_process_alive, _redact_dsn, get_gobby_home
from gobby.code_index.bm25_health import render_bm25_status, repair_bm25_indexes
from gobby.config.app import load_config
from gobby.utils.json_helpers import json_dumps


@click.group("postgres")
def postgres_cli() -> None:
    """Manage the local PostgreSQL hub database."""


@postgres_cli.command("install")
def install_cmd() -> None:
    """Install or configure PostgreSQL."""
    result = install_postgres()
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
    """Show PostgreSQL health, extension, and ownership status."""
    payload = asyncio.run(get_postgres_status())
    if as_json:
        click.echo(json_dumps(payload, indent=2, sort_keys=True))
    else:
        click.echo(render_postgres_status(payload))


@postgres_cli.command("repair-code-index")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the repair payload as JSON on stdout.",
)
def repair_code_index_cmd(as_json: bool) -> None:
    """Verify and selectively rebuild damaged code-index BM25 indexes."""
    database_url = _read_bootstrap_database_url(get_gobby_home())
    if not database_url:
        raise click.ClickException(
            "PostgreSQL credentials are unavailable; run `gobby postgres install` first."
        )
    timeout = load_config(resolve_database_url=False).code_index.maintenance_index_timeout_seconds
    payload = repair_bm25_indexes(database_url, timeout_seconds=timeout)
    if as_json:
        click.echo(json_dumps(payload, indent=2, sort_keys=True))
    else:
        click.echo("\n".join(render_bm25_status(payload)))
    if not payload["healthy"]:
        raise click.exceptions.Exit(1)


@postgres_cli.command("backup")
@click.option(
    "--output",
    "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Backup directory. Defaults to ~/.gobby/backups/postgres/<UTC timestamp>/.",
)
def backup_cmd(output_dir: Path | None) -> None:
    """Create a verified logical PostgreSQL backup."""
    if _daemon_running():
        raise click.ClickException("Stop the daemon first: gobby stop")

    result = create_postgres_backup(output_dir=output_dir, gobby_home=get_gobby_home())
    _render_backup_result(result)


@postgres_cli.command("restore")
@click.argument("dump_or_dir", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--clean",
    is_flag=True,
    default=False,
    help="Drop database objects before restoring them from the dump.",
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Restore without an interactive confirmation prompt.",
)
@click.option(
    "--allow-unverified",
    is_flag=True,
    default=False,
    help="Allow restore when metadata.json and SHA256SUMS are missing.",
)
def restore_cmd(dump_or_dir: Path, clean: bool, yes: bool, allow_unverified: bool) -> None:
    """Restore a verified PostgreSQL backup into the configured target database."""
    if _daemon_running():
        raise click.ClickException("Stop the daemon first: gobby stop")
    if not yes and not click.confirm(
        "Restore PostgreSQL backup into the configured target database?"
    ):
        click.echo("Aborted.")
        sys.exit(0)

    result = restore_postgres_backup(
        dump_or_dir,
        clean=clean,
        allow_unverified=allow_unverified,
        gobby_home=get_gobby_home(),
    )
    _render_restore_result(result)


def _render_install_result(result: dict[str, Any]) -> None:
    if result.get("success"):
        click.echo(result.get("message", "PostgreSQL configured"))
        if result.get("database_url"):
            click.echo(f"  DSN:  {_redact_dsn(str(result['database_url']))}")
        if result.get("compose_file"):
            click.echo(f"  Compose: {result['compose_file']}")
        pgaudit_available = result.get("pgaudit_available")
        if pgaudit_available is not None:
            click.echo(f"  pgaudit available: {'yes' if pgaudit_available else 'no'}")
        click.echo("\nRestart the daemon to use the updated PostgreSQL settings.")
        return

    click.echo(f"Failed: {result.get('error', 'unknown error')}", err=True)
    sys.exit(1)


def _render_backup_result(result: dict[str, Any]) -> None:
    click.echo(f"PostgreSQL backup created: {result.get('backup_dir', '<unknown>')}")
    if dump_path := result.get("dump_path"):
        click.echo(f"  Dump:       {dump_path}")
    if metadata_path := result.get("metadata_path"):
        click.echo(f"  Metadata:   {metadata_path}")
    if sha256s_path := result.get("sha256s_path"):
        click.echo(f"  SHA256SUMS: {sha256s_path}")
    if dump_sha256 := result.get("dump_sha256"):
        click.echo(f"  SHA256:     {dump_sha256}")
    if result.get("verified"):
        click.echo("  Verified: pg_restore --list")
    if result.get("sha256_verified"):
        click.echo("  Verified: SHA256SUMS")


def _render_restore_result(result: dict[str, Any]) -> None:
    probes = cast(dict[str, Any], result.get("probes", {}))
    click.echo("PostgreSQL restore completed.")
    if database_url := result.get("database_url"):
        click.echo(f"  Target:    {database_url}")
    if dump_sha256 := result.get("dump_sha256"):
        click.echo(f"  SHA256:    {dump_sha256}")
    if result.get("sha256_verified"):
        click.echo("  Verified: SHA256SUMS")
    click.echo(f"  pg_search: {'yes' if probes.get('pg_search_present') else 'no'}")
    click.echo(f"  pgaudit:   {'yes' if probes.get('pgaudit_present') else 'no'}")
    click.echo(f"  pgcrypto:  {'yes' if probes.get('pgcrypto_present') else 'no'}")


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
