"""
Export and import Gobby resources (workflows, agents, prompts) between projects.

Provides CLI commands for sharing customized resources across projects
or backing them up to the global ~/.gobby/ directory.
"""

from pathlib import Path
from shutil import copy2

import click
import yaml
from pydantic import ValidationError
from yaml import YAMLError

from gobby.prompts.models import parse_frontmatter
from gobby.workflows.definitions import AgentDefinitionBody, PipelineDefinition, WorkflowDefinition

# Resource types and their directory names
RESOURCE_TYPES = {
    "workflow": "workflows",
    "agent": "agents",
    "prompt": "prompts",
}

IMPORT_EXTENSIONS = {
    "workflow": {".yaml", ".yml"},
    "agent": {".yaml", ".yml"},
    "prompt": {".md"},
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


def _list_import_resources(source_dir: Path, resource_type: str) -> list[Path]:
    """List importable resource files in a directory (recursively)."""
    allowed_extensions = IMPORT_EXTENSIONS[resource_type]
    return [
        path for path in _list_resources(source_dir) if path.suffix.lower() in allowed_extensions
    ]


def _validate_import_extension(resource_type: str, source: Path) -> None:
    """Reject unsupported import file extensions."""
    if source.suffix.lower() not in IMPORT_EXTENSIONS[resource_type]:
        allowed = ", ".join(sorted(IMPORT_EXTENSIONS[resource_type]))
        raise click.ClickException(
            f"Unsupported {resource_type} import file extension '{source.suffix}'. "
            f"Expected one of: {allowed}."
        )


def _read_yaml_mapping(source: Path) -> dict[str, object]:
    """Read a YAML file that must contain a mapping."""
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise click.ClickException(f"Invalid UTF-8 in {source}: {exc}") from exc
    except YAMLError as exc:
        raise click.ClickException(f"Invalid YAML in {source}: {exc}") from exc

    if not isinstance(data, dict):
        raise click.ClickException(f"Invalid YAML in {source}: expected a mapping.")
    return data


def _validate_workflow_import(source: Path) -> None:
    """Validate workflow YAML with the workflow definition schemas."""
    data = _read_yaml_mapping(source)
    schema_cls = PipelineDefinition if data.get("type") == "pipeline" else WorkflowDefinition
    try:
        schema_cls.model_validate(data)
    except ValidationError as exc:
        raise click.ClickException(f"Invalid workflow definition in {source}: {exc}") from exc


def _validate_agent_import(source: Path) -> None:
    """Validate agent YAML with the agent definition schema."""
    data = _read_yaml_mapping(source)
    raw_name = data.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        data["name"] = source.stem
    try:
        AgentDefinitionBody.model_validate(data)
    except ValidationError as exc:
        raise click.ClickException(f"Invalid agent definition in {source}: {exc}") from exc


def _validate_prompt_import(source: Path) -> None:
    """Validate prompt markdown with the prompt frontmatter parser."""
    try:
        content = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise click.ClickException(f"Invalid UTF-8 in {source}: {exc}") from exc

    parse_frontmatter(content)


def _validate_import_resource(resource_type: str, source: Path) -> None:
    """Validate one import source against its resource parser/schema."""
    _validate_import_extension(resource_type, source)
    if resource_type == "workflow":
        _validate_workflow_import(source)
    elif resource_type == "agent":
        _validate_agent_import(source)
    else:
        _validate_prompt_import(source)


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
                _validate_import_resource(rtype, source)
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
                for ext in sorted(IMPORT_EXTENSIONS[rtype]):
                    candidate = source_dir / f"{name}{ext}"
                    if candidate.exists():
                        specific = candidate
                        break
            if not specific.exists():
                continue
            if specific.is_file():
                files = [specific] if specific.suffix.lower() in IMPORT_EXTENSIONS[rtype] else []
            else:
                files = _list_import_resources(specific, rtype)
        else:
            files = _list_import_resources(source_dir, rtype)

        if not files:
            continue

        for f in files:
            _validate_import_resource(rtype, f)

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
