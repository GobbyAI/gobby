"""Hub-local files-home CLI."""

from __future__ import annotations

import json

import click

from gobby.config.bootstrap import BootstrapConfigError
from gobby.files_migrate import FilesMigrateError, run_files_migrate
from gobby.paths import FilesHomeError, FilesHomeNotOnThisDaemonError
from gobby.runner_pid_file import SingletonError


@click.group("files")
def files() -> None:
    """Operate on the hub-owned files home."""


@files.command("migrate")
def migrate() -> None:
    """Move leftover hub files into the provisioned files_home."""
    try:
        report = run_files_migrate()
    except FilesHomeNotOnThisDaemonError as exc:
        raise click.ClickException("files migrate is hub-local only") from exc
    except (
        FilesHomeError,
        BootstrapConfigError,
        FilesMigrateError,
        SingletonError,
    ) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(report.to_dict(), indent=2))
