"""Daemon-backed skills CLI operations."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any, Protocol

import click

from gobby.utils.daemon_client import DaemonClient
from gobby.utils.json_helpers import json_dumps

DaemonClientFactory = Callable[[click.Context], DaemonClient]
DaemonChecker = Callable[[DaemonClient], bool]


class SkillToolCaller(Protocol):
    """Callable protocol for invoking gobby-skills MCP tools."""

    def __call__(
        self,
        client: DaemonClient,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float = 30.0,
    ) -> Any | None: ...


def _checked_client(
    ctx: click.Context,
    client_factory: DaemonClientFactory,
    daemon_checker: DaemonChecker,
) -> DaemonClient:
    client = client_factory(ctx)
    if not daemon_checker(client):
        sys.exit(1)
    return client


def install_skill(
    ctx: click.Context,
    client_factory: DaemonClientFactory,
    daemon_checker: DaemonChecker,
    call_tool: SkillToolCaller,
    source: str,
    project: bool,
) -> None:
    """Install a skill from a source."""
    client = _checked_client(ctx, client_factory, daemon_checker)
    result = call_tool(
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
    if result.get("success"):
        click.echo(
            f"Installed skill: {result.get('skill_name', '<unknown>')} "
            f"({result.get('source_type', 'unknown')})"
        )
        return

    click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)
    sys.exit(1)


def remove_skill(
    ctx: click.Context,
    client_factory: DaemonClientFactory,
    daemon_checker: DaemonChecker,
    call_tool: SkillToolCaller,
    name: str,
) -> None:
    """Remove an installed skill."""
    client = _checked_client(ctx, client_factory, daemon_checker)
    result = call_tool(client, "remove_skill", {"name": name})

    if result is None:
        click.echo("Error: Failed to communicate with daemon", err=True)
        sys.exit(1)
    if result.get("success"):
        click.echo(f"Removed skill: {result.get('skill_name', name)}")
        return

    click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)
    sys.exit(1)


def update_skill(
    ctx: click.Context,
    client_factory: DaemonClientFactory,
    daemon_checker: DaemonChecker,
    call_tool: SkillToolCaller,
    name: str | None,
    update_all: bool,
) -> None:
    """Update an installed skill from its source."""
    client = _checked_client(ctx, client_factory, daemon_checker)

    if not name and not update_all:
        click.echo("Error: Provide a skill name or use --all to update all skills")
        sys.exit(1)

    if update_all:
        _update_all_skills(client, call_tool)
        return

    result = call_tool(client, "update_skill", {"name": name})

    if result is None:
        click.echo("Error: Failed to communicate with daemon", err=True)
        sys.exit(1)
    if result.get("success"):
        if result.get("updated"):
            click.echo(f"Updated skill: {name}")
        else:
            click.echo(f"Skipped: {result.get('skip_reason') or 'already up to date'}")
        return

    click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)
    sys.exit(1)


def _update_all_skills(client: DaemonClient, call_tool: SkillToolCaller) -> None:
    result = call_tool(client, "list_skills", {"limit": 1000})
    if not result or not result.get("success"):
        click.echo(
            f"Error: {result.get('error', 'Failed to list skills') if result else 'No response'}",
            err=True,
        )
        sys.exit(1)

    updated = 0
    skipped = 0
    for skill in result.get("skills", []):
        update_result = call_tool(client, "update_skill", {"name": skill["name"]})
        if update_result and update_result.get("success"):
            if update_result.get("updated"):
                click.echo(f"Updated: {skill['name']}")
                updated += 1
            else:
                click.echo(
                    f"Skipped: {skill['name']} ({update_result.get('skip_reason') or 'up to date'})"
                )
                skipped += 1
        else:
            click.echo(f"Failed: {skill['name']}")
            skipped += 1

    click.echo(f"\nUpdated {updated} skill(s), skipped {skipped}")


def search_hub(
    ctx: click.Context,
    client_factory: DaemonClientFactory,
    daemon_checker: DaemonChecker,
    call_tool: SkillToolCaller,
    query: str,
    hub_name: str | None,
    limit: int,
    json_output: bool,
) -> None:
    """Search for skills across configured hubs."""
    client = _checked_client(ctx, client_factory, daemon_checker)

    arguments: dict[str, Any] = {"query": query, "limit": limit}
    if hub_name:
        arguments["hub_name"] = hub_name

    result = call_tool(client, "search_hub", arguments)

    if result is None:
        click.echo("Error: Failed to communicate with daemon", err=True)
        sys.exit(1)
    if not result.get("success"):
        click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)
        sys.exit(1)

    results_list = result.get("results", [])

    if json_output:
        click.echo(json_dumps(results_list, indent=2))
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
