"""Skills CLI commands.

This module provides CLI commands for managing skills:
- list: List all installed skills
- show: Show details of a specific skill
- install: Install a skill from a source
- remove: Remove an installed skill
"""

from __future__ import annotations

import json
from typing import Any

import click
import httpx

from gobby.cli._skills_daemon import (
    install_skill as _install_skill,
)
from gobby.cli._skills_daemon import (
    remove_skill as _remove_skill,
)
from gobby.cli._skills_daemon import (
    search_hub as _search_hub,
)
from gobby.cli._skills_daemon import (
    update_skill as _update_skill,
)
from gobby.cli._skills_hubs import add_hub as _add_hub
from gobby.cli._skills_hubs import list_hubs as _list_hubs
from gobby.cli._skills_local import generate_docs as _generate_docs
from gobby.cli._skills_local import list_skills as _list_skills
from gobby.cli._skills_local import output_json as _default_output_json
from gobby.cli._skills_local import set_skill_enabled as _set_skill_enabled
from gobby.cli._skills_local import show_skill as _show_skill
from gobby.cli._skills_metadata import get_metadata as _get_metadata
from gobby.cli._skills_metadata import set_metadata as _set_metadata
from gobby.cli._skills_metadata import unset_metadata as _unset_metadata
from gobby.cli._skills_scaffold import create_skill as _create_skill
from gobby.cli._skills_scaffold import init_skills as _init_skills
from gobby.cli._skills_validation import validate_skill as _validate_skill
from gobby.cli.runtime import get_cli_runtime, require_cli_database
from gobby.cli.utils_config import get_daemon_client as _shared_daemon_client
from gobby.skills import metadata as skills_metadata
from gobby.storage.skills import LocalSkillManager
from gobby.utils.daemon_client import DaemonClient

get_nested_value = skills_metadata.get_nested_value
get_skill_category = skills_metadata.get_skill_category
get_skill_tags = skills_metadata.get_skill_tags
set_nested_value = skills_metadata.set_nested_value
unset_nested_value = skills_metadata.unset_nested_value


def get_skill_storage() -> LocalSkillManager:
    """Get skill storage manager."""
    db = require_cli_database()
    return LocalSkillManager(db)


def get_daemon_client(ctx: click.Context) -> DaemonClient:
    """Get daemon client from context config."""
    get_cli_runtime(ctx)
    return _shared_daemon_client()


def call_skills_tool(
    client: DaemonClient,
    tool_name: str,
    arguments: dict[str, Any],
    timeout: float = 30.0,
) -> Any | None:
    """Call a gobby-skills MCP tool via the daemon.

    Returns the inner result from the MCP response, or None on communication error.
    The daemon strips the ``success`` marker from successful internal tool
    results, so dict results get it restored here for the CLI handlers that
    key on it; an inner ``success: False`` payload is kept as-is.
    """
    try:
        response = client.call_mcp_tool(
            server_name="gobby-skills",
            tool_name=tool_name,
            arguments=arguments,
            timeout=timeout,
        )
        if response.get("success") and "result" in response:
            result = response["result"]
            if isinstance(result, dict):
                return {"success": True, **result}
            return result

        error = response.get("error") or response.get("message") or "MCP call failed"
        click.echo(f"Error: {error}", err=True)
        if isinstance(response, dict):
            return dict(response)
        return None
    except (ConnectionError, httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        return None


def check_daemon(client: DaemonClient) -> bool:
    """Check if daemon is running."""
    is_healthy, _error = client.check_health()
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
    _list_skills(get_skill_storage, _output_json, category, tags, enabled, limit, json_output)


def _output_json(skills_list: list[Any]) -> None:
    """Output skills as JSON."""
    _default_output_json(skills_list)


@skills.command()
@click.argument("name")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.pass_context
def show(ctx: click.Context, name: str, json_output: bool) -> None:
    """Show details of a specific skill."""
    _show_skill(get_skill_storage, name, json_output)


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
    _install_skill(ctx, get_daemon_client, check_daemon, call_skills_tool, source, project)


@skills.command()
@click.argument("name")
@click.pass_context
def remove(ctx: click.Context, name: str) -> None:
    """Remove an installed skill.

    NAME is the skill name to remove (e.g., 'commit-message').

    Requires daemon to be running.
    """
    _remove_skill(ctx, get_daemon_client, check_daemon, call_skills_tool, name)


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
    _update_skill(ctx, get_daemon_client, check_daemon, call_skills_tool, name, update_all)


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
    _validate_skill(path, json_output)


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
    _get_metadata(get_skill_storage, name, key)


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
    _set_metadata(get_skill_storage, name, key, value)


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
    _unset_metadata(get_skill_storage, name, key)


@skills.command()
@click.pass_context
def init(ctx: click.Context) -> None:
    """Initialize skills directory for the current project.

    Creates .gobby/skills/ directory and config file for local skill management.
    This is idempotent - running init multiple times is safe.
    """
    _init_skills()


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
    _create_skill(name, description)


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
    _generate_docs(get_skill_storage, output, output_format)


@skills.command()
@click.argument("name")
@click.pass_context
def enable(ctx: click.Context, name: str) -> None:
    """Enable a skill.

    NAME is the skill name to enable.
    """
    _set_skill_enabled(get_skill_storage, name, True)


@skills.command()
@click.argument("name")
@click.pass_context
def disable(ctx: click.Context, name: str) -> None:
    """Disable a skill.

    NAME is the skill name to disable.
    """
    _set_skill_enabled(get_skill_storage, name, False)


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
    _search_hub(
        ctx,
        get_daemon_client,
        check_daemon,
        call_skills_tool,
        query,
        hub_name,
        limit,
        json_output,
    )


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
    _list_hubs(ctx, get_daemon_client, check_daemon, call_skills_tool, json_output)


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
    _add_hub(require_cli_database(ctx), name, hub_type, base_url, repo, branch, auth_key_name)
