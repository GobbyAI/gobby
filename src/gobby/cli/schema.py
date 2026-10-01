"""Schema migration commands."""

from __future__ import annotations

import click

from gobby.storage.schema_contract import installed_schema_version


@click.group("schema")
def schema() -> None:
    """Inspect or apply the hub schema migration chain."""


@schema.command("apply")
@click.pass_context
def apply_schema(ctx: click.Context) -> None:
    """Apply every pending migration."""
    from gobby.cli.runtime import get_cli_runtime

    get_cli_runtime(ctx).require_database()
    click.echo(f"Schema is at version {installed_schema_version()}")
