"""Skill hub CLI operations."""

from __future__ import annotations

import sys
from typing import Any

import click

from gobby.cli._skills_daemon import (
    DaemonChecker,
    DaemonClientFactory,
    SkillToolCaller,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.json_helpers import json_dumps

VALID_HUB_TYPES = ["clawdhub", "skillsmp", "github", "claude-plugins"]


def list_hubs(
    ctx: click.Context,
    client_factory: DaemonClientFactory,
    daemon_checker: DaemonChecker,
    call_tool: SkillToolCaller,
    json_output: bool,
) -> None:
    """List configured skill hubs."""
    client = client_factory(ctx)
    if not daemon_checker(client):
        sys.exit(1)

    result = call_tool(client, "list_hubs", {})

    if result is None:
        click.echo("Error: Failed to communicate with daemon", err=True)
        sys.exit(1)
    if not result.get("success"):
        click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)
        sys.exit(1)

    hubs_list = result.get("hubs", [])

    if json_output:
        click.echo(json_dumps(hubs_list, indent=2))
        return

    if not hubs_list:
        click.echo("No hubs configured.")
        click.echo("\nTo add hubs, use: gobby skills hub add <name> --type <type>")
        return

    click.echo("Configured hubs:\n")
    for hub in hubs_list:
        name = hub.get("name", "unknown")
        hub_type = hub.get("type", "unknown")
        base_url = hub.get("base_url", "")
        url_str = f" ({base_url})" if base_url else ""
        click.echo(f"  {name} [{hub_type}]{url_str}")


def add_hub(
    db: HubDatabase,
    name: str,
    hub_type: str,
    base_url: str | None,
    repo: str | None,
    branch: str | None,
    auth_key_name: str | None,
) -> None:
    """Add a new skill hub."""
    _validate_hub_options(hub_type, base_url, repo)
    hub_config = _build_hub_config(hub_type, base_url, repo, branch, auth_key_name)
    _store_hub_config(db, name, hub_config)

    click.echo(f"Added hub: {name} [{hub_type}]")
    click.echo("\nRestart the daemon for changes to take effect: gobby restart")


def _validate_hub_options(hub_type: str, base_url: str | None, repo: str | None) -> None:
    if hub_type not in VALID_HUB_TYPES:
        click.echo(
            f"Error: Invalid hub type '{hub_type}'. Must be one of: {', '.join(VALID_HUB_TYPES)}",
            err=True,
        )
        sys.exit(1)

    if hub_type == "skillsmp" and not base_url:
        click.echo("Error: --url is required for skillsmp type", err=True)
        sys.exit(1)

    if hub_type == "claude-plugins" and not base_url:
        click.echo("Error: --url is required for claude-plugins type", err=True)
        sys.exit(1)

    if hub_type == "github" and not repo:
        click.echo("Error: --repo is required for github type", err=True)
        sys.exit(1)


def _build_hub_config(
    hub_type: str,
    base_url: str | None,
    repo: str | None,
    branch: str | None,
    auth_key_name: str | None,
) -> dict[str, Any]:
    hub_config: dict[str, Any] = {"type": hub_type}
    if base_url:
        hub_config["base_url"] = base_url
    if repo:
        hub_config["repo"] = repo
    if branch:
        hub_config["branch"] = branch
    if auth_key_name:
        hub_config["auth_key_name"] = auth_key_name
    return hub_config


def _store_hub_config(
    db: HubDatabase,
    name: str,
    hub_config: dict[str, Any],
) -> None:
    try:
        from gobby.storage.config_mutations import ConfigPatch
        from gobby.storage.config_store import ConfigStore

        store = ConfigStore(db)
        snapshot = store.read_snapshot()
        existing = snapshot.overrides.get(f"skills.hubs.{name}.type")
        if existing is not None:
            click.echo(
                f"Error: Hub '{name}' already exists. Use 'hub remove' first to replace it.",
                err=True,
            )
            sys.exit(1)

        store.patch(
            expected_revision=snapshot.revision,
            patch=ConfigPatch(
                values={f"skills.hubs.{name}.{key}": value for key, value in hub_config.items()}
            ),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        click.echo(f"Error: Failed to save hub config: {exc}", err=True)
        sys.exit(1)
