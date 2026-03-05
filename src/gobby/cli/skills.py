"""Skills CLI commands.

This module provides CLI commands for managing skills:
- list: List all installed skills
- show: Show details of a specific skill
- install: Install a skill from a source
- remove: Remove an installed skill
"""

import json
import sys
from pathlib import Path
from typing import Any

import click

from gobby.config.app import DaemonConfig
from gobby.skills.metadata import (
    get_nested_value,
    get_skill_category,
    get_skill_tags,
    set_nested_value,
    unset_nested_value,
)
from gobby.storage.database import LocalDatabase
from gobby.storage.skills import LocalSkillManager
from gobby.utils.daemon_client import DaemonClient


def get_skill_storage() -> LocalSkillManager:
    """Get skill storage manager."""
    db = LocalDatabase()
    return LocalSkillManager(db)


def get_daemon_client(ctx: click.Context) -> DaemonClient:
    """Get daemon client from context config."""
    if ctx.obj is None or "config" not in ctx.obj:
        raise click.ClickException(
            "Configuration not initialized. Ensure the CLI is invoked through the main entry point."
        )
    config = ctx.obj.get("config")
    if not isinstance(config, DaemonConfig):
        raise click.ClickException(
            f"Invalid configuration type: expected DaemonConfig, got {type(config).__name__}"
        )
    return DaemonClient(host="localhost", port=config.daemon_port)


def call_skills_tool(
    client: DaemonClient,
    tool_name: str,
    arguments: dict[str, Any],
    timeout: float = 30.0,
) -> dict[str, Any] | None:
    """Call a gobby-skills MCP tool via the daemon.

    Returns the inner result from the MCP response, or None on error.
    """
    try:
        response = client.call_mcp_tool(
            server_name="gobby-skills",
            tool_name=tool_name,
            arguments=arguments,
            timeout=timeout,
        )
        # Response format is {"success": true, "result": {...}}
        # Extract the inner result for the caller
        if response.get("success") and "result" in response:
            result = response["result"]
            return dict(result) if isinstance(result, dict) else None
        # If outer call failed, return None and log error
        click.echo("Error: MCP call failed", err=True)
        return None
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        return None


def check_daemon(client: DaemonClient) -> bool:
    """Check if daemon is running."""
    is_healthy, error = client.check_health()
    if not is_healthy:
        click.echo("Error: Daemon not running. Start with: gobby start", err=True)
        return False
    return True


@click.group()
def skills() -> None:
    """Manage Gobby skills."""
    pass


@skills.command("list")
@click.option("--category", "-c", help="Filter by category")
@click.option("--tags", "-t", help="Filter by tags (comma-separated)")
@click.option("--enabled/--disabled", default=None, help="Filter by enabled status")
@click.option("--limit", "-n", default=50, help="Maximum skills to show")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.pass_context
def list_skills(
    ctx: click.Context,
    category: str | None,
    tags: str | None,
    enabled: bool | None,
    limit: int,
    json_output: bool,
) -> None:
    """List installed skills."""
    storage = get_skill_storage()

    # When filtering by tags, fetch all skills first, then filter and apply limit
    # This ensures the limit applies to filtered results, not pre-filter
    fetch_limit = 10000 if tags else limit

    skills_list = storage.list_skills(
        category=category,
        enabled=enabled,
        limit=fetch_limit,
        include_global=True,
    )

    # Filter by tags if specified
    if tags:
        tags_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tags_list:
            filtered_skills = []
            for skill in skills_list:
                skill_tags = get_skill_tags(skill)
                if any(tag in skill_tags for tag in tags_list):
                    filtered_skills.append(skill)
            # Apply limit after tag filtering
            skills_list = filtered_skills[:limit]

    if json_output:
        _output_json(skills_list)
        return

    if not skills_list:
        click.echo("No skills found.")
        return

    for skill in skills_list:
        # Get category from metadata if available
        cat_str = ""
        skill_category = get_skill_category(skill)
        if skill_category:
            cat_str = f" [{skill_category}]"

        status = "✓" if skill.enabled else "✗"
        desc = skill.description[:60] if skill.description else ""
        click.echo(f"{status} {skill.name}{cat_str} - {desc}")


def _output_json(skills_list: list[Any]) -> None:
    """Output skills as JSON."""
    from gobby.skills.formatting import format_skills_json

    click.echo(format_skills_json(skills_list))


@skills.command()
@click.argument("name")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.pass_context
def show(ctx: click.Context, name: str, json_output: bool) -> None:
    """Show details of a specific skill."""
    storage = get_skill_storage()
    skill = storage.get_by_name(name)

    if skill is None:
        if json_output:
            click.echo(json.dumps({"error": "Skill not found", "name": name}))
        else:
            click.echo(f"Skill not found: {name}")
        sys.exit(1)

    if json_output:
        output = {
            "name": skill.name,
            "description": skill.description,
            "version": skill.version,
            "license": skill.license,
            "enabled": skill.enabled,
            "source_type": skill.source_type,
            "source_path": skill.source_path,
            "compatibility": skill.compatibility if hasattr(skill, "compatibility") else None,
            "content": skill.content,
            "category": get_skill_category(skill),
            "tags": get_skill_tags(skill),
        }
        click.echo(json.dumps(output, indent=2))
        return

    click.echo(f"Name: {skill.name}")
    click.echo(f"Description: {skill.description}")
    if skill.version:
        click.echo(f"Version: {skill.version}")
    if skill.license:
        click.echo(f"License: {skill.license}")
    click.echo(f"Enabled: {skill.enabled}")
    if skill.source_type:
        click.echo(f"Source: {skill.source_type}")
    if skill.source_path:
        click.echo(f"Path: {skill.source_path}")
    click.echo("")
    click.echo("Content:")
    click.echo("-" * 40)
    click.echo(skill.content)


@skills.command()
@click.argument("source")
@click.option("--project", "-p", is_flag=True, help="Install scoped to project")
@click.pass_context
def install(ctx: click.Context, source: str, project: bool) -> None:
    """Install a skill from a source.

    SOURCE can be:
    - A hub reference (e.g., clawdhub:commit-message, skillsmp:code-review)
    - A local directory path (e.g., ./my-skill or /path/to/skill)
    - A path to a SKILL.md file (e.g., ./SKILL.md)
    - A GitHub URL (owner/repo, github:owner/repo, https://github.com/owner/repo)
    - A ZIP archive path (e.g., ./skills.zip)

    Use 'gobby skills hub list' to see available hubs.
    Use 'gobby skills search <query>' to find skills.

    Use --project to scope the skill to the current project.

    Requires daemon to be running.
    """
    client = get_daemon_client(ctx)
    if not check_daemon(client):
        sys.exit(1)

    result = call_skills_tool(
        client,
        "install_skill",
        {
            "source": source,
            "project_scoped": project,
        },
    )

    if result is None:
        click.echo("Error: Failed to communicate with daemon", err=True)
        sys.exit(1)
    elif result.get("success"):
        click.echo(
            f"Installed skill: {result.get('skill_name', '<unknown>')} ({result.get('source_type', 'unknown')})"
        )
    else:
        click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)
        sys.exit(1)


@skills.command()
@click.argument("name")
@click.pass_context
def remove(ctx: click.Context, name: str) -> None:
    """Remove an installed skill.

    NAME is the skill name to remove (e.g., 'commit-message').

    Requires daemon to be running.
    """
    client = get_daemon_client(ctx)
    if not check_daemon(client):
        sys.exit(1)

    result = call_skills_tool(client, "remove_skill", {"name": name})

    if result is None:
        click.echo("Error: Failed to communicate with daemon", err=True)
        sys.exit(1)
    elif result.get("success"):
        click.echo(f"Removed skill: {result.get('skill_name', name)}")
    else:
        click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)
        sys.exit(1)


@skills.command()
@click.argument("name", required=False)
@click.option("--all", "update_all", is_flag=True, help="Update all installed skills")
@click.pass_context
def update(ctx: click.Context, name: str | None, update_all: bool) -> None:
    """Update an installed skill from its source.

    NAME is the skill name to update (e.g., 'commit-message').
    Use --all to update all skills that have remote sources.

    Only skills installed from GitHub can be updated (re-fetched from source).
    Local skills are skipped.

    Requires daemon to be running.
    """
    client = get_daemon_client(ctx)
    if not check_daemon(client):
        sys.exit(1)

    if not name and not update_all:
        click.echo("Error: Provide a skill name or use --all to update all skills")
        sys.exit(1)

    if update_all:
        # Get all skills and update each via MCP
        result = call_skills_tool(client, "list_skills", {"limit": 1000})
        if not result or not result.get("success"):
            click.echo(
                f"Error: {result.get('error', 'Failed to list skills') if result else 'No response'}",
                err=True,
            )
            sys.exit(1)

        updated = 0
        skipped = 0
        for skill in result.get("skills", []):
            update_result = call_skills_tool(client, "update_skill", {"name": skill["name"]})
            if update_result and update_result.get("success"):
                if update_result.get("updated"):
                    click.echo(f"Updated: {skill['name']}")
                    updated += 1
                else:
                    click.echo(
                        f"Skipped: {skill['name']} ({update_result.get('skip_reason', 'up to date')})"
                    )
                    skipped += 1
            else:
                click.echo(f"Failed: {skill['name']}")
                skipped += 1

        click.echo(f"\nUpdated {updated} skill(s), skipped {skipped}")
        return

    # Single skill update
    result = call_skills_tool(client, "update_skill", {"name": name})

    if result is None:
        click.echo("Error: Failed to communicate with daemon", err=True)
        sys.exit(1)
    elif result.get("success"):
        if result.get("updated"):
            click.echo(f"Updated skill: {name}")
        else:
            click.echo(f"Skipped: {result.get('skip_reason', 'already up to date')}")
    else:
        click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)
        sys.exit(1)


@skills.command()
@click.argument("path")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.pass_context
def validate(ctx: click.Context, path: str, json_output: bool) -> None:
    """Validate a SKILL.md file against the Agent Skills specification.

    PATH is the path to a SKILL.md file or directory containing one.

    Validates:
    - name: max 64 chars, lowercase + hyphens only
    - description: max 1024 chars, non-empty
    - version: semver pattern (if provided)
    - category: lowercase alphanumeric + hyphens (if provided)
    - tags: list of strings, each max 64 chars (if provided)
    """
    from gobby.skills.loader import SkillLoader, SkillLoadError
    from gobby.skills.validator import SkillValidator

    source_path = Path(path)

    if not source_path.exists():
        if json_output:
            click.echo(json.dumps({"error": "Path not found", "path": path}))
        else:
            click.echo(f"Error: Path not found: {path}")
        sys.exit(1)

    # Load the skill
    loader = SkillLoader()
    try:
        # Don't validate during load - we want to do it ourselves
        parsed_skill = loader.load_skill(source_path, validate=False, check_dir_name=False)
    except SkillLoadError as e:
        if json_output:
            click.echo(json.dumps({"error": str(e), "path": path}))
        else:
            click.echo(f"Error loading skill: {e}")
        sys.exit(1)

    # Validate the skill
    validator = SkillValidator()
    result = validator.validate(parsed_skill)

    if json_output:
        output = result.to_dict()
        output["path"] = path
        output["skill_name"] = parsed_skill.name
        click.echo(json.dumps(output, indent=2))
        if not result.valid:
            sys.exit(1)
        return

    # Human-readable output
    if result.valid:
        click.echo(f"✓ Valid: {parsed_skill.name}")
        if result.warnings:
            click.echo("\nWarnings:")
            for warning in result.warnings:
                click.echo(f"  - {warning}")
    else:
        click.echo(f"✗ Invalid: {parsed_skill.name}")
        click.echo("\nErrors:")
        for error in result.errors:
            click.echo(f"  - {error}")
        if result.warnings:
            click.echo("\nWarnings:")
            for warning in result.warnings:
                click.echo(f"  - {warning}")
        sys.exit(1)


# Meta subcommand group
@skills.group()
def meta() -> None:
    """Manage skill metadata fields."""
    pass


@meta.command("get")
@click.argument("name")
@click.argument("key")
@click.pass_context
def meta_get(ctx: click.Context, name: str, key: str) -> None:
    """Get a metadata field value.

    NAME is the skill name.
    KEY is the metadata field (supports dot notation for nested keys).

    Examples:
        gobby skills meta get my-skill author
        gobby skills meta get my-skill skillport.category
    """
    storage = get_skill_storage()
    skill = storage.get_by_name(name)

    if skill is None:
        click.echo(f"Skill not found: {name}", err=True)
        sys.exit(1)

    if not skill.metadata:
        click.echo("null")
        return

    value = get_nested_value(skill.metadata, key)
    if value is None:
        click.echo(f"Key not found: {key}")
        sys.exit(1)
    elif isinstance(value, dict | list):
        click.echo(json.dumps(value, indent=2))
    else:
        click.echo(str(value))


@meta.command("set")
@click.argument("name")
@click.argument("key")
@click.argument("value")
@click.pass_context
def meta_set(ctx: click.Context, name: str, key: str, value: str) -> None:
    """Set a metadata field value.

    NAME is the skill name.
    KEY is the metadata field (supports dot notation for nested keys).
    VALUE is the value to set.

    Examples:
        gobby skills meta set my-skill author "John Doe"
        gobby skills meta set my-skill skillport.category git
    """
    storage = get_skill_storage()
    skill = storage.get_by_name(name)

    if skill is None:
        click.echo(f"Skill not found: {name}", err=True)
        sys.exit(1)

    # Try to parse value as JSON for complex types
    try:
        parsed_value = json.loads(value)
    except json.JSONDecodeError:
        parsed_value = value

    new_metadata = set_nested_value(skill.metadata or {}, key, parsed_value)
    try:
        storage.update_skill(skill.id, metadata=new_metadata)
    except Exception as e:
        click.echo(f"Error updating skill metadata: {e}", err=True)
        sys.exit(1)
    click.echo(f"Set {key} = {value}")


@meta.command("unset")
@click.argument("name")
@click.argument("key")
@click.pass_context
def meta_unset(ctx: click.Context, name: str, key: str) -> None:
    """Remove a metadata field.

    NAME is the skill name.
    KEY is the metadata field (supports dot notation for nested keys).

    Examples:
        gobby skills meta unset my-skill author
        gobby skills meta unset my-skill skillport.tags
    """
    storage = get_skill_storage()
    skill = storage.get_by_name(name)

    if skill is None:
        click.echo(f"Skill not found: {name}", err=True)
        sys.exit(1)

    if not skill.metadata:
        click.echo(f"Key not found: {key}")
        return

    new_metadata = unset_nested_value(skill.metadata, key)
    try:
        storage.update_skill(skill.id, metadata=new_metadata)
    except Exception as e:
        click.echo(f"Error updating skill metadata: {e}", err=True)
        sys.exit(1)
    click.echo(f"Unset {key}")


@skills.command()
@click.pass_context
def init(ctx: click.Context) -> None:
    """Initialize skills directory for the current project.

    Creates .gobby/skills/ directory and config file for local skill management.
    This is idempotent - running init multiple times is safe.
    """
    from gobby.skills.scaffold import init_skills_directory

    base_path = Path(".")
    skills_dir = base_path / ".gobby" / "skills"
    config_file = skills_dir / "config.yaml"

    result = init_skills_directory(base_path)

    if result["dir_created"]:
        click.echo(f"Created {skills_dir}/")
    else:
        click.echo(f"Skills directory already exists: {skills_dir}/")

    if result["config_created"]:
        click.echo(f"Created {config_file}")
    else:
        click.echo(f"Config already exists: {config_file}")

    click.echo("\nSkills initialized successfully!")


@skills.command()
@click.argument("name")
@click.option("--description", "-d", default=None, help="Skill description")
@click.pass_context
def new(ctx: click.Context, name: str, description: str | None) -> None:
    """Create a new skill scaffold.

    NAME is the skill name (lowercase, hyphens allowed).

    Creates a new skill directory with:
    - SKILL.md with frontmatter template
    - scripts/ directory for helper scripts
    - assets/ directory for images and files
    - references/ directory for documentation
    """
    from gobby.skills.scaffold import scaffold_skill

    try:
        scaffold_skill(name, Path("."), description)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except FileExistsError as e:
        click.echo(str(e), err=True)
        sys.exit(1)

    click.echo(f"Created skill scaffold: {name}/")
    click.echo(f"  - {name}/SKILL.md")
    click.echo(f"  - {name}/scripts/")
    click.echo(f"  - {name}/assets/")
    click.echo(f"  - {name}/references/")


@skills.command()
@click.option("--output", "-o", default=None, help="Output file path")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["markdown", "json"]),
    default="markdown",
    help="Output format",
)
@click.pass_context
def doc(ctx: click.Context, output: str | None, output_format: str) -> None:
    """Generate documentation for installed skills.

    Creates a markdown table or JSON list of all installed skills.
    Use --output to write to a file instead of stdout.
    """
    storage = get_skill_storage()
    skills_list = storage.list_skills(include_global=True)

    if not skills_list:
        click.echo("No skills installed.")
        return

    from gobby.skills.formatting import format_skills_json, format_skills_markdown_table

    if output_format == "json":
        content = format_skills_json(skills_list)
    else:
        content = format_skills_markdown_table(skills_list)

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(content)
        click.echo(f"Written to {output}")
    else:
        click.echo(content)


@skills.command()
@click.argument("name")
@click.pass_context
def enable(ctx: click.Context, name: str) -> None:
    """Enable a skill.

    NAME is the skill name to enable.
    """
    storage = get_skill_storage()
    skill = storage.get_by_name(name)

    if skill is None:
        click.echo(f"Skill not found: {name}", err=True)
        sys.exit(1)

    try:
        storage.update_skill(skill.id, enabled=True)
    except Exception as e:
        click.echo(f"Error enabling skill: {e}", err=True)
        sys.exit(1)
    click.echo(f"Enabled skill: {name}")


@skills.command()
@click.argument("name")
@click.pass_context
def disable(ctx: click.Context, name: str) -> None:
    """Disable a skill.

    NAME is the skill name to disable.
    """
    storage = get_skill_storage()
    skill = storage.get_by_name(name)

    if skill is None:
        click.echo(f"Skill not found: {name}", err=True)
        sys.exit(1)

    try:
        storage.update_skill(skill.id, enabled=False)
    except Exception as e:
        click.echo(f"Error disabling skill: {e}", err=True)
        sys.exit(1)
    click.echo(f"Disabled skill: {name}")


@skills.command()
@click.argument("query")
@click.option("--hub", "-h", "hub_name", default=None, help="Search only in specific hub")
@click.option("--limit", "-n", default=20, help="Maximum results to show")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.pass_context
def search(
    ctx: click.Context,
    query: str,
    hub_name: str | None,
    limit: int,
    json_output: bool,
) -> None:
    """Search for skills across configured hubs.

    QUERY is the search term (e.g., 'commit message', 'code review').

    Use --hub to search only in a specific hub.

    Requires daemon to be running.
    """
    client = get_daemon_client(ctx)
    if not check_daemon(client):
        sys.exit(1)

    arguments: dict[str, Any] = {"query": query, "limit": limit}
    if hub_name:
        arguments["hub_name"] = hub_name

    result = call_skills_tool(client, "search_hub", arguments)

    if result is None:
        click.echo("Error: Failed to communicate with daemon", err=True)
        sys.exit(1)
    elif not result.get("success"):
        click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)
        sys.exit(1)

    results_list = result.get("results", [])

    if json_output:
        click.echo(json.dumps(results_list, indent=2))
        return

    if not results_list:
        click.echo("No skills found matching your query.")
        return

    click.echo(f"Found {len(results_list)} skill(s):\n")
    for skill in results_list:
        hub = skill.get("hub_name", "unknown")
        slug = skill.get("slug", "unknown")
        name = skill.get("display_name", slug)
        desc = skill.get("description", "")[:60]
        click.echo(f"  [{hub}] {name}")
        if desc:
            click.echo(f"          {desc}")
        click.echo(f"          Install: gobby skills install {hub}:{slug}")
        click.echo("")


# Hub subcommand group
@skills.group()
def hub() -> None:
    """Manage skill hubs (registries)."""
    pass


@hub.command("list")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.pass_context
def hub_list(ctx: click.Context, json_output: bool) -> None:
    """List configured skill hubs.

    Shows all configured skill hubs with their type and status.

    Requires daemon to be running.
    """
    client = get_daemon_client(ctx)
    if not check_daemon(client):
        sys.exit(1)

    result = call_skills_tool(client, "list_hubs", {})

    if result is None:
        click.echo("Error: Failed to communicate with daemon", err=True)
        sys.exit(1)
    elif not result.get("success"):
        click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)
        sys.exit(1)

    hubs_list = result.get("hubs", [])

    if json_output:
        click.echo(json.dumps(hubs_list, indent=2))
        return

    if not hubs_list:
        click.echo("No hubs configured.")
        click.echo("\nTo add hubs, use: gobby skills hub add <name> --type <type>")
        return

    click.echo("Configured hubs:\n")
    for h in hubs_list:
        name = h.get("name", "unknown")
        hub_type = h.get("type", "unknown")
        base_url = h.get("base_url", "")
        url_str = f" ({base_url})" if base_url else ""
        click.echo(f"  {name} [{hub_type}]{url_str}")


@hub.command("add")
@click.argument("name")
@click.option(
    "--type",
    "hub_type",
    required=True,
    help="Hub type (clawdhub, skillsmp, github, claude-plugins)",
)
@click.option("--url", "base_url", default=None, help="Base URL for skillsmp/claude-plugins type")
@click.option("--repo", default=None, help="GitHub repo (owner/repo) for github type")
@click.option("--branch", default=None, help="Branch for github type (default: main)")
@click.option(
    "--auth-key", "auth_key_name", default=None, help="Environment variable name for auth key"
)
@click.pass_context
def hub_add(
    ctx: click.Context,
    name: str,
    hub_type: str,
    base_url: str | None,
    repo: str | None,
    branch: str | None,
    auth_key_name: str | None,
) -> None:
    """Add a new skill hub.

    NAME is the hub name (e.g., 'my-skills', 'company-hub').

    Hub types:
    - clawdhub: ClawdHub CLI-based hub
    - skillsmp: SkillsMP marketplace (requires --url, optional --auth-key)
    - github: GitHub repository collection (requires --repo)
    - claude-plugins: Claude Plugins directory (requires --url)

    Examples:
        gobby skills hub add my-skillsmp --type skillsmp --url https://skillsmp.com/api/v1
        gobby skills hub add company-skills --type github --repo myorg/skills
    """

    # Validate hub type
    valid_types = ["clawdhub", "skillsmp", "github", "claude-plugins"]
    if hub_type not in valid_types:
        click.echo(
            f"Error: Invalid hub type '{hub_type}'. Must be one of: {', '.join(valid_types)}",
            err=True,
        )
        sys.exit(1)

    # Validate required options for each type
    if hub_type == "skillsmp" and not base_url:
        click.echo("Error: --url is required for skillsmp type", err=True)
        sys.exit(1)

    if hub_type == "claude-plugins" and not base_url:
        click.echo("Error: --url is required for claude-plugins type", err=True)
        sys.exit(1)

    if hub_type == "github" and not repo:
        click.echo("Error: --repo is required for github type", err=True)
        sys.exit(1)

    # Build hub config
    hub_config: dict[str, Any] = {"type": hub_type}
    if base_url:
        hub_config["base_url"] = base_url
    if repo:
        hub_config["repo"] = repo
    if branch:
        hub_config["branch"] = branch
    if auth_key_name:
        hub_config["auth_key_name"] = auth_key_name

    # Write hub config via ConfigStore (DB-first)
    try:
        from gobby.cli.utils import load_full_config_from_db
        from gobby.storage.config_store import ConfigStore
        from gobby.storage.database import LocalDatabase

        config = load_full_config_from_db()
        db_path = Path(config.database_path).expanduser()
        db = LocalDatabase(db_path)
        try:
            store = ConfigStore(db)
            # Check if hub already exists
            existing = store.get(f"skills.hubs.{name}.type")
            if existing is not None:
                click.echo(
                    f"Error: Hub '{name}' already exists. Use 'hub remove' first to replace it.",
                    err=True,
                )
                sys.exit(1)

            # Write each hub config field as a dotted key
            for key, value in hub_config.items():
                store.set(f"skills.hubs.{name}.{key}", value, source="cli")
        finally:
            db.close()
    except Exception as e:
        click.echo(f"Error: Failed to save hub config: {e}", err=True)
        sys.exit(1)

    click.echo(f"Added hub: {name} [{hub_type}]")
    click.echo("\nRestart the daemon for changes to take effect: gobby restart")
