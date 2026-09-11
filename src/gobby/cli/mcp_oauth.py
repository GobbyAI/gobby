"""Interactive OAuth login for registered remote MCP servers."""

import asyncio
import webbrowser
from urllib.parse import quote, urlsplit

import click
from mcp.client import Client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from gobby.cli.mcp_proxy import (
    call_mcp_api,
    check_daemon_running,
    get_daemon_client,
    mcp_proxy,
    resolve_cli_mcp_project,
)
from gobby.cli.runtime import require_cli_database
from gobby.mcp_proxy.models import MCPServerConfig
from gobby.mcp_proxy.oauth import MCPOAuthStorage, PersistentOAuthProvider
from gobby.mcp_proxy.oauth_callback import OAuthCallback
from gobby.mcp_proxy.transports.http import build_mcp_http_client
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.secrets import SecretStore


async def authorize_server(config: MCPServerConfig, store: SecretStore, timeout: float) -> None:
    storage = MCPOAuthStorage(store, config)
    await storage.load()
    port = 0
    if storage.state.client and storage.state.client.redirect_uris:
        redirect = urlsplit(str(storage.state.client.redirect_uris[0]))
        if (
            redirect.scheme != "http"
            or redirect.hostname != "127.0.0.1"
            or redirect.path != "/callback"
            or not redirect.port
            or redirect.query
            or redirect.fragment
            or redirect.username
        ):
            raise click.ClickException("Stored OAuth client has an invalid loopback callback URI")
        port = redirect.port

    async def open_browser(url: str) -> None:
        click.echo(f"Authorize {config.name} in your browser:\n{url}")
        await asyncio.to_thread(webbrowser.open, url)

    async with asyncio.timeout(timeout), OAuthCallback(open_browser, port) as callback:
        auth = PersistentOAuthProvider(
            config, storage, callback.redirect_uri, callback.redirect, callback.wait
        )
        if config.url is None:
            raise ValueError("OAuth requires a server URL")
        async with build_mcp_http_client(config.headers, auth) as http_client:
            transport = (
                sse_client(config.url, headers=config.headers, auth=auth)
                if config.transport == "sse"
                else streamable_http_client(config.url, http_client=http_client)
            )
            async with Client(transport) as client:
                # Some servers permit initialization anonymously and challenge discovery.
                await client.list_tools()
        if storage.state.tokens is None:
            raise click.ClickException(
                "The MCP server did not request OAuth during initialization or tool discovery; "
                "check the server URL and authentication requirements"
            )


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
    try:
        config.validate()
        asyncio.run(authorize_server(config, store, timeout))
    except click.ClickException:
        raise
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
