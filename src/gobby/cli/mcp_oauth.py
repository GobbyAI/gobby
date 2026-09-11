"""Interactive OAuth login for registered remote MCP servers."""

import asyncio
import webbrowser
from urllib.parse import quote

import click

from gobby.cli.mcp_proxy import (
    call_mcp_api,
    check_daemon_running,
    get_daemon_client,
    mcp_proxy,
    resolve_cli_mcp_project,
)
from gobby.cli.runtime import require_cli_database
from gobby.mcp_proxy.models import MCPError, MCPServerConfig
from gobby.mcp_proxy.oauth import authorize_server
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.secrets import SecretStore


@mcp_proxy.command("auth")
@click.argument("name")
@click.option("--global", "global_scope", is_flag=True, help="Authorize a machine-wide instance")
@click.option("--timeout", type=click.FloatRange(min=1), default=300, show_default=True)
@click.pass_context
def auth_server(ctx: click.Context, name: str, global_scope: bool, timeout: float) -> None:
    """Sign in to an HTTP/SSE MCP server in your browser."""
    client = get_daemon_client(ctx)
    if not check_daemon_running(client):
        raise click.ClickException("Gobby daemon is not running")
    project_id, scope = resolve_cli_mcp_project(global_scope=global_scope)
    db = require_cli_database()
    row = LocalMCPManager(db).get_server(name, project_id=project_id or GLOBAL_PROJECT_ID)
    if row is None:
        raise click.ClickException(f"No MCP server {name!r} in {scope} scope")
    store = SecretStore(db)
    config = MCPServerConfig(
        name=row.name,
        id=row.id,
        project_id=row.project_id,
        transport=row.transport,
        url=row.url,
        requires_oauth=True,
        headers=store.resolve_dict(row.headers, project_id=row.project_id) if row.headers else None,
    )

    async def open_browser(url: str) -> None:
        click.echo(f"Authorize {config.name} in your browser:\n{url}")
        await asyncio.to_thread(webbrowser.open, url)

    try:
        config.validate()
        asyncio.run(authorize_server(config, store, timeout, open_browser))
    except click.ClickException:
        raise
    except MCPError as exc:
        raise click.ClickException(str(exc)) from exc
    except TimeoutError as exc:
        raise click.ClickException(
            "OAuth authorization timed out; run auth again to retry"
        ) from exc
    except Exception as exc:
        # SDK failures can contain token response bodies; keep credentials out of CLI errors.
        raise click.ClickException(f"OAuth authorization failed ({type(exc).__name__})") from exc

    payload: dict[str, object] = {"requires_oauth": True, "scope": scope}
    if project_id:
        payload["project_id"] = project_id
    result = call_mcp_api(
        client, f"/api/mcp/servers/{quote(name, safe='')}", method="PUT", json_data=payload
    )
    if not result or not result.get("success"):
        raise click.ClickException("Credentials saved, but updating the daemon connection failed")
    click.echo(f"Authorized MCP server: {name}")
