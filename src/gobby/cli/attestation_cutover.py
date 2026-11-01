"""One-time evidence cutover, invoked with python -m gobby.cli.attestation_cutover."""

import hashlib
import os
from pathlib import Path
from uuid import UUID

import click

from gobby.cli.runtime import CliRuntime, require_cli_database
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.jsonl_io import atomic_write_bytes
from gobby.sync.tasks import TaskBackupManager
from gobby.tasks.attestation_conversion import (
    ConversionBackup,
    apply_conversion,
    prepare_conversion,
    restore_conversion,
    save_conversion_backup,
)


class CutoverBackup(ConversionBackup):
    task_backup_destination: str
    archive_sha256: str


@click.command()
@click.argument("action", type=click.Choice(["prepare", "apply", "restore"]))
@click.option("--project-id", type=click.UUID, required=True)
@click.option("--backup", type=click.Path(path_type=Path), required=True)
@click.option("--task-backup", type=click.Path(path_type=Path), required=True)
@click.pass_context
def main(
    ctx: click.Context, action: str, project_id: UUID, backup: Path, task_backup: Path
) -> None:
    """Convert one project's evidence while its writers and closes are paused.

    PREPARE saves immutable row and JSONL snapshots without changing task data.
    APPLY converts the rows and regenerates TASK_BACKUP before releasing the pause.
    RESTORE rolls back both snapshots only while no new evidence has been written.
    Keep writers paused after any failure; retry APPLY to finish backup regeneration.
    """
    ctx = ctx.find_root()
    if not isinstance(ctx.obj, CliRuntime):
        runtime = CliRuntime(config_file=None)
        ctx.obj = runtime
        ctx.call_on_close(runtime.close)
    try:
        backup = backup.resolve()
        archive = backup.with_name(backup.name + ".tasks.jsonl").resolve()
        task_backup = task_backup.resolve()
        if len({backup, archive, task_backup}) != 3:
            raise ValueError("Conversion snapshots and task backup must be distinct paths")
        db = require_cli_database(apply_migrations=False)
        if action == "prepare":
            if backup.exists() or archive.exists():
                raise FileExistsError(
                    "Conversion snapshots already exist; choose a new backup path"
                )
            source = task_backup.read_bytes() if task_backup.exists() else b""
            prepared = CutoverBackup(
                **prepare_conversion(db, project_id=str(project_id)).model_dump(),
                task_backup_destination=str(task_backup),
                archive_sha256=hashlib.sha256(source).hexdigest(),
            )
            save_conversion_backup(prepared, backup)
            descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(source)
                stream.flush()
                os.fsync(stream.fileno())
            directory = os.open(archive.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            click.echo(f"Prepared {len(prepared.source_rows)} evidence rows; task data unchanged")
            return

        saved = CutoverBackup.model_validate_json(backup.read_bytes())
        if saved.project_id != str(project_id):
            raise ValueError("Conversion backup belongs to another project")
        if saved.task_backup_destination != str(task_backup):
            raise ValueError("Task backup destination differs from the prepared destination")
        original_jsonl = archive.read_bytes()
        if hashlib.sha256(original_jsonl).hexdigest() != saved.archive_sha256:
            raise ValueError("Task backup archive differs from the prepared snapshot")
        if action == "apply":
            count = apply_conversion(db, saved)
            TaskBackupManager(LocalTaskManager(db), task_backup).backup(str(project_id))
            click.echo(f"Converted {count} evidence rows; regenerated task backup")
        else:
            count = restore_conversion(db, saved)
            atomic_write_bytes(task_backup, original_jsonl)
            click.echo(f"Restored {count} original evidence rows and original task backup")
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    main()
