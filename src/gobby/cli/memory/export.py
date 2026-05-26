from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import click

from gobby.sync.export_context import in_jsonl_export_context


def _facade() -> ModuleType:
    return importlib.import_module("gobby.cli.memory")


def _default_backup_path(project_ctx: dict[str, object] | None) -> Path:
    project_path = project_ctx.get("project_path") if project_ctx else None
    root = Path(str(project_path)).expanduser().resolve() if project_path else Path.cwd().resolve()
    return root / ".gobby" / "memories.jsonl"


@click.command("export")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.option(
    "--output", "-o", "output_file", type=click.Path(), help="Output file (stdout if not specified)"
)
@click.option("--no-metadata", is_flag=True, help="Exclude memory metadata")
@click.option("--no-stats", is_flag=True, help="Exclude summary statistics")
@click.pass_context
def export_memories(
    ctx: click.Context,
    project_ref: str | None,
    output_file: str | None,
    no_metadata: bool,
    no_stats: bool,
) -> None:
    """Export memories as markdown.

    Exports all memories (or filtered by project) to a formatted markdown document.
    Output goes to stdout by default, or to a file with --output.

    Examples:

        gobby memory export                    # Export all to stdout

        gobby memory export -o memories.md    # Export to file

        gobby memory export -p myproject      # Export specific project

        gobby memory export --no-metadata     # Content only, no metadata
    """
    memory_module = _facade()
    project_id = memory_module.resolve_project_ref(project_ref) if project_ref else None
    manager = memory_module.get_memory_manager(ctx)

    markdown = manager.export_markdown(
        project_id=project_id,
        include_metadata=not no_metadata,
        include_stats=not no_stats,
    )

    if output_file:
        path = Path(output_file)
        try:
            path.write_text(markdown, encoding="utf-8")
            click.echo(f"Exported memories to {output_file}")
        except OSError as e:
            raise click.ClickException(f"Failed to write to {output_file}: {e}") from e
    else:
        click.echo(markdown)


@click.command("backup")
@click.option(
    "--output",
    "-o",
    "output_path",
    type=click.Path(),
    help="Output file path (default: .gobby/memories.jsonl)",
)
@click.option("--quiet", "-q", is_flag=True, help="Suppress output")
@click.pass_context
def backup_memories(ctx: click.Context, output_path: str | None, quiet: bool) -> None:
    """Backup memories to JSONL file.

    Exports project-scoped memories to a JSONL file for backup/disaster recovery.
    This runs synchronously and can be used even when the daemon is not running.

    Examples:

        gobby memory backup                           # Export to .gobby/memories.jsonl

        gobby memory backup -o ~/backups/mem.jsonl   # Export to custom path
    """
    from gobby.config.persistence import MemoryBackupConfig
    from gobby.sync.memories import MemoryBackupManager
    from gobby.utils.project_context import get_project_context

    project_ctx = get_project_context(cwd=Path.cwd())
    raw_project_id = project_ctx.get("id") if project_ctx else None
    project_id = str(raw_project_id) if raw_project_id else None

    if not output_path and not in_jsonl_export_context():
        if not quiet:
            click.echo(
                "Skipping memory backup: .gobby/memories.jsonl is generated only during "
                "remote push."
            )
        return

    memory_module = _facade()
    manager = memory_module.get_memory_manager(ctx)

    if output_path:
        export_path = Path(output_path).expanduser().resolve()
    else:
        export_path = _default_backup_path(project_ctx)

    config = MemoryBackupConfig(enabled=True, export_path=export_path)
    backup_mgr = MemoryBackupManager(
        db=manager.db,
        memory_manager=manager,
        config=config,
    )

    count = backup_mgr.backup_sync(project_id=project_id)
    if not quiet:
        if count > 0:
            click.echo(f"Backed up {count} memories to {export_path}")
        else:
            click.echo("No memories to backup.")


@click.command("restore")
@click.option(
    "--input",
    "input_path",
    type=click.Path(),
    help="Input file path (default: .gobby/memories.jsonl)",
)
@click.option("--quiet", "-q", is_flag=True, help="Suppress output")
@click.option("--force", is_flag=True, help="Import even when the database has as many memories")
@click.pass_context
def restore_memories(ctx: click.Context, input_path: str | None, quiet: bool, force: bool) -> None:
    """Restore memories from a JSONL backup file.

    Imports memories from a JSONL file into the database. This runs synchronously
    and is the explicit CLI path for reading .gobby/memories.jsonl.

    Examples:

        gobby memory restore

        gobby memory restore --input ~/backups/mem.jsonl

        gobby memory restore --force
    """
    from gobby.utils.project_context import get_project_context

    project_ctx = get_project_context(cwd=Path.cwd())
    restore_path = (
        Path(input_path).expanduser().resolve() if input_path else _default_backup_path(project_ctx)
    )
    if not restore_path.is_file():
        if input_path:
            raise click.ClickException(f"Memory backup not found: {restore_path}")
        if not quiet:
            click.echo(f"No memory backup found at {restore_path}")
        return

    from gobby.config.persistence import MemoryBackupConfig
    from gobby.sync.memories import MemoryBackupManager

    memory_module = _facade()
    manager = memory_module.get_memory_manager(ctx)
    config = MemoryBackupConfig(enabled=True, export_path=restore_path)
    backup_mgr = MemoryBackupManager(
        db=manager.db,
        memory_manager=manager,
        config=config,
    )

    count = backup_mgr.import_sync(force=force)
    if not quiet:
        if count > 0:
            click.echo(f"Restored {count} memories from {restore_path}")
        else:
            click.echo("No memories restored.")
