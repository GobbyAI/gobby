"""Template listing commands for `gobby mcp-proxy`."""

from __future__ import annotations

import json
import sys
from typing import Any

import click

import gobby.cli.mcp_proxy as _mcp
from gobby.cli.mcp_proxy import mcp_proxy
from gobby.utils.json_helpers import json_dumps


@mcp_proxy.command("list-templates")
@click.option("--global", "global_scope", is_flag=True, help="List global templates")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def list_templates(ctx: click.Context, global_scope: bool, json_format: bool) -> None:
    """List MCP server templates visible to this project."""
    client = _mcp.get_daemon_client(ctx)
    if not _mcp.check_daemon_running(client):
        sys.exit(1)
    project_id, scope = _mcp.resolve_cli_mcp_project(global_scope=global_scope)
    params = f"scope={scope}"
    if project_id:
        params += f"&project_id={project_id}"
    result = _mcp.call_mcp_api(client, f"/api/mcp/templates?{params}", method="GET")
    if result is None:
        sys.exit(1)
    if json_format:
        click.echo(json_dumps(result, indent=2))
        return
    templates = result.get("templates") or []
    if not templates:
        click.echo("No MCP templates visible.")
        return
    for item in templates:
        name = item.get("name", "")
        click.echo(f"{name}  {item.get('scope', '')}  {item.get('owner', '')}")


@mcp_proxy.command("show-template")
@click.argument("name")
@click.option("--global", "global_scope", is_flag=True, help="Look up a global template")
@click.pass_context
def show_template(ctx: click.Context, name: str, global_scope: bool) -> None:
    """Print one template's parameter contract."""
    client = _mcp.get_daemon_client(ctx)
    if not _mcp.check_daemon_running(client):
        sys.exit(1)
    project_id, scope = _mcp.resolve_cli_mcp_project(global_scope=global_scope)
    params = f"scope={scope}"
    if project_id:
        params += f"&project_id={project_id}"
    result = _mcp.call_mcp_api(client, f"/api/mcp/templates?{params}", method="GET")
    if result is None:
        sys.exit(1)
    match: dict[str, Any] | None = None
    for item in result.get("templates") or []:
        if item.get("name") == name:
            match = item
            break
    if match is None:
        click.echo(f"Error: template '{name}' not found", err=True)
        sys.exit(1)
    click.echo(json.dumps(match, indent=2))
    for param in match.get("params") or []:
        click.echo(str(param.get("name") or ""))
