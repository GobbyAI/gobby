"""
Export and import Gobby resources (workflows, agents, prompts) between projects.

Provides CLI commands for sharing customized resources across projects
or backing them up to the global ~/.gobby/ directory.
"""

from pathlib import Path
from shutil import copy2

import click

# Resource types and their directory names
RESOURCE_TYPES = {
    "workflow": "workflows",
    "agent": "agents",
    "prompt": "prompts",
}


def _get_project_resource_dir(resource_type: str) -> Path:
    """Get the .gobby/ resource directory for a resource type in the current project."""
    return Path.cwd() / ".gobby" / RESOURCE_TYPES[resource_type]


def _resolve_target_dir(resource_type: str, to: str | None, global_: bool) -> Path | None:
    """Resolve the target directory for export."""
    if global_:
        return Path.home() / ".gobby" / RESOURCE_TYPES[resource_type]
    if to:
        return Path(to) / ".gobby" / RESOURCE_TYPES[resource_type]
    return None


def _resolve_import_destination(target_dir: Path, dest_name: str) -> Path:
    """Resolve a single-file import destination within the target resource directory."""
    dest_path = Path(dest_name)
    if dest_path.is_absolute() or ".." in dest_path.parts:
        raise click.ClickException("Import name must be relative and cannot contain '..'.")

    target_root = target_dir.resolve()
    dest = (target_dir / dest_path).resolve()
    if not dest.is_relative_to(target_root):
        raise click.ClickException("Import destination must stay within the target directory.")
    return dest


def _list_resources(source_dir: Path) -> list[Path]:
    """List all resource files in a directory (recursively)."""
    if not source_dir.exists():
        return []
    results: list[Path] = []
    for item in sorted(source_dir.rglob("*")):
        if item.is_file():
            results.append(item)
    return results


def _copy_resource(source: Path, target_dir: Path, source_base: Path) -> str:
    """Copy a single resource file, preserving subdirectory structure."""
    rel = source.relative_to(source_base)
    dest = target_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    copy2(source, dest)
    return str(rel)


@click.group()
def export_import() -> None:
    """Export and import Gobby resources."""
    pass


@click.command("export")
@click.argument("type_", metavar="TYPE", type=click.Choice(list(RESOURCE_TYPES) + ["all"]))
@click.argument("name", required=False, default=None)
@click.option("--to", "to_path", type=click.Path(), help="Target project path to export to.")
@click.option("--global", "global_", is_flag=True, help="Export to ~/.gobby/ (global).")
@click.option(
    "--dry-run", "dry_run_flag", is_flag=True, help="Perform a dry run without writing files."
)
def export_cmd(
    type_: str, name: str | None, to_path: str | None, global_: bool, dry_run_flag: bool
) -> None:
    """Export resources from the current project.

    TYPE is one of: workflow, agent, prompt, all.

    Without --to or --global, performs a dry run showing what would be exported.
    """
    types_to_export = list(RESOURCE_TYPES) if type_ == "all" else [type_]
    dry_run = dry_run_flag or (not to_path and not global_)

    if dry_run:
        click.echo("Dry run (pass --to <path> or --global to actually export):\n")

    total = 0
    for rtype in types_to_export:
        source_dir = _get_project_resource_dir(rtype)
        if not source_dir.exists():
            continue

        # If a name is given, narrow to that specific file/subdir
        if name:
            specific = source_dir / name
            # Try with extension
            if not specific.exists():
                for ext in (".yaml", ".yml", ".md"):
                    candidate = source_dir / f"{name}{ext}"
                    if candidate.exists():
                        specific = candidate
                        break
            if not specific.exists():
                continue
            if specific.is_file():
                files = [specific]
            else:
                files = _list_resources(specific)
        else:
            files = _list_resources(source_dir)

        if not files:
            continue

        target_dir = _resolve_target_dir(rtype, to_path, global_)

        click.echo(f"{RESOURCE_TYPES[rtype]}:")
        for f in files:
            rel = f.relative_to(source_dir)
            if dry_run:
                click.echo(f"  {rel}")
            else:
                if target_dir is None:
                    raise click.ClickException("Target directory could not be resolved")
                copied = _copy_resource(f, target_dir, source_dir)
                click.echo(f"  {copied} -> {target_dir / copied}")
            total += 1

    if total == 0:
        click.echo("No resources found to export.")
    elif dry_run:
        click.echo(f"\n{total} file(s) would be exported.")
    else:
        click.echo(f"\n{total} file(s) exported.")


@click.command("import")
@click.argument("type_", metavar="TYPE", type=click.Choice(list(RESOURCE_TYPES) + ["all"]))
@click.argument("name", required=False, default=None)
@click.option(
    "--from", "from_path", type=click.Path(exists=True), help="File or directory to import."
)
@click.option(
    "--from-project",
    "from_project",
    type=click.Path(exists=True),
    help="Import from another project's .gobby/ directory.",
)
def import_cmd(
    type_: str, name: str | None, from_path: str | None, from_project: str | None
) -> None:
    """Import resources into the current project.

    TYPE is one of: workflow, agent, prompt, all.
    """
    if not from_path and not from_project:
        raise click.ClickException("Specify --from <path> or --from-project <path>.")
    if from_path and from_project:
        raise click.ClickException("Cannot specify both --from and --from-project.")

    types_to_import = list(RESOURCE_TYPES) if type_ == "all" else [type_]
    total = 0

    for rtype in types_to_import:
        target_dir = _get_project_resource_dir(rtype)

        if from_project:
            source_dir = Path(from_project) / ".gobby" / RESOURCE_TYPES[rtype]
        elif from_path:
            source = Path(from_path)
            if source.is_file():
                # Import a single file directly
                dest_name = name or source.name
                dest = _resolve_import_destination(target_dir, dest_name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    if not click.confirm(f"Overwrite {dest}?"):
                        continue
                copy2(source, dest)
                click.echo(f"  {dest_name} -> {dest}")
                total += 1
                continue
            else:
                source_dir = source
        else:
            continue

        if not source_dir.exists():
            continue

        # If a name is given, narrow to specific file/subdir
        if name:
            specific = source_dir / name
            if not specific.exists():
                for ext in (".yaml", ".yml", ".md"):
                    candidate = source_dir / f"{name}{ext}"
                    if candidate.exists():
                        specific = candidate
                        break
            if not specific.exists():
                continue
            if specific.is_file():
                files = [specific]
            else:
                files = _list_resources(specific)
        else:
            files = _list_resources(source_dir)

        if not files:
            continue

        click.echo(f"{RESOURCE_TYPES[rtype]}:")
        for f in files:
            rel = f.relative_to(source_dir)
            dest = target_dir / rel
            if dest.exists():
                if not click.confirm(f"  Overwrite {rel}?"):
                    continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            copy2(f, dest)
            click.echo(f"  {rel}")
            total += 1

    if total == 0:
        click.echo("No resources found to import.")
    else:
        click.echo(f"\n{total} file(s) imported.")
