"""
MCP proxy CLI commands.

Provides CLI access to MCP proxy functionality:
- list-servers: List configured MCP servers
- list-tools: List tools from MCP servers
- get-schema: Get full schema for a specific tool
- call-tool: Execute a tool on an MCP server
- add-server: Add a new MCP server configuration
- remove-server: Remove an MCP server configuration
- recommend-tools: Get AI-powered tool recommendations
"""

import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any, cast

import click

from gobby.cli.runtime import require_cli_database
from gobby.cli.utils_config import get_daemon_client as _shared_daemon_client
from gobby.utils.daemon_client import DaemonClient, DaemonHealthError
from gobby.utils.json_helpers import json_dumps


def get_daemon_client(ctx: click.Context) -> DaemonClient:
    """Get daemon client from context config."""
    from gobby.cli.runtime import get_cli_runtime

    try:
        get_cli_runtime(ctx)
    except RuntimeError as exc:
        raise click.ClickException("Daemon config is unavailable") from exc
    return _shared_daemon_client()


def check_daemon_running(client: DaemonClient) -> bool:
    """Check if daemon is running and print error if not."""
    is_healthy, error = client.check_health()
    if not is_healthy:
        if error is DaemonHealthError.NOT_RUNNING:
            click.echo("Error: Gobby daemon is not running. Start it with: gobby start", err=True)
        else:
            click.echo(f"Error: Cannot connect to daemon: {error}", err=True)
        return False
    return True


def resolve_cli_mcp_project(*, global_scope: bool) -> tuple[str | None, str]:
    """Return (project_id, scope) for a scoped mcp-proxy command."""
    if global_scope:
        return None, "global"
    from gobby.cli.installers.shared import registered_project_id

    project_id = registered_project_id(require_cli_database(), Path.cwd())
    if not project_id:
        raise click.ClickException(
            "not inside a registered Gobby project; pass --global for a machine-wide instance"
        )
    return project_id, "project"


def _cwd_project_id() -> str | None:
    """Best-effort registered-project id for the current directory."""
    from gobby.cli.installers.shared import registered_project_id
    from gobby.cli.runtime import require_cli_database

    try:
        return registered_project_id(require_cli_database(), Path.cwd())
    except Exception:
        return None


def _stdin_is_tty() -> bool:
    isatty = getattr(sys.stdin, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def store_cli_secret(
    name: str,
    value: str,
    *,
    project_id: str | None,
    global_scope: bool,
) -> None:
    """Store a secret in the instance's scope via the secrets store."""
    from gobby.storage.config_store import ConfigStore
    from gobby.storage.secrets import SecretStore

    db = require_cli_database()
    ConfigStore(db).set_named_secret(
        SecretStore(db),
        name,
        value,
        category="general",
        description=None,
        project_id=None if global_scope else project_id,
    )


def call_mcp_api(
    client: DaemonClient,
    endpoint: str,
    method: str = "POST",
    json_data: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any] | None:
    """Call MCP API endpoint and handle errors."""
    try:
        response = client.call_http_api(
            endpoint, method=method, json_data=json_data, timeout=timeout
        )
        if response.status_code == 200:
            return cast(dict[str, Any], response.json())
        else:
            error_msg = response.text or f"HTTP {response.status_code}"
            click.echo(f"Error: {error_msg}", err=True)
            return None
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        return None


@click.group("mcp-proxy")
def mcp_proxy() -> None:
    """Manage MCP proxy servers and tools."""
    pass


@mcp_proxy.command("list-servers")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def list_servers(ctx: click.Context, json_format: bool) -> None:
    """List all configured MCP servers."""
    client = get_daemon_client(ctx)
    if not check_daemon_running(client):
        sys.exit(1)

    endpoint = "/api/mcp/servers"
    project_id = _cwd_project_id()
    if project_id:
        endpoint = f"{endpoint}?project_id={project_id}"
    result = call_mcp_api(client, endpoint, method="GET")
    if result is None:
        sys.exit(1)

    servers = result.get("servers", [])

    if json_format:
        click.echo(json_dumps(result, indent=2))
        return

    if not servers:
        click.echo("No MCP servers configured.")
        return

    connected = result.get("connected", 0)
    total = result.get("total", 0)
    click.echo(f"MCP Servers ({connected}/{total} connected):")
    click.echo("NAME  SCOPE  TEMPLATE  STATE")
    for server in servers:
        state = server.get("state", "unknown")
        scope = server.get("scope", "")
        template = server.get("template") or ""
        click.echo(f"{server['name']}  {scope}  {template}  {state}")


@mcp_proxy.command("list-tools")
@click.option("--server", "-s", help="Filter by server name")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def list_tools(ctx: click.Context, server: str | None, json_format: bool) -> None:
    """List tools from MCP servers."""
    client = get_daemon_client(ctx)
    if not check_daemon_running(client):
        sys.exit(1)

    endpoint = "/api/mcp/tools"
    if server:
        encoded_server = urllib.parse.quote(server)
        endpoint = f"/api/mcp/tools?server_filter={encoded_server}"

    result = call_mcp_api(client, endpoint, method="GET")
    if result is None:
        sys.exit(1)

    if json_format:
        click.echo(json_dumps(result, indent=2))
        return

    tools_by_server = result.get("tools", {})
    if not tools_by_server:
        click.echo("No tools available.")
        return

    for server_name, tools in tools_by_server.items():
        click.echo(f"\n{server_name}:")
        if not tools:
            click.echo("  (no tools)")
            continue
        for tool in tools:
            name = tool.get("name", "unknown")
            brief = tool.get("brief", tool.get("description", ""))[:60]
            click.echo(f"  • {name}")
            if brief:
                click.echo(f"    {brief}")


@mcp_proxy.command("get-schema")
@click.argument("server_name")
@click.argument("tool_name")
@click.pass_context
def get_schema(ctx: click.Context, server_name: str, tool_name: str) -> None:
    """Get full schema for a specific tool.

    Examples:
        gobby mcp-proxy get-schema context7 get-library-docs
        gobby mcp-proxy get-schema supabase list_tables
    """
    client = get_daemon_client(ctx)
    if not check_daemon_running(client):
        sys.exit(1)

    result = call_mcp_api(
        client,
        "/api/mcp/tools/schema",
        method="POST",
        json_data={"server_name": server_name, "tool_name": tool_name},
    )
    if result is None:
        sys.exit(1)

    # Always output as JSON for schema (it's complex)
    click.echo(json_dumps(result, indent=2))


@mcp_proxy.command("call-tool")
@click.argument("server_name")
@click.argument("tool_name")
@click.option("--arg", "-a", "args", multiple=True, help="Tool argument in key=value format")
@click.option("--json-args", "-j", "json_args", help="Tool arguments as JSON string")
@click.option("--raw", is_flag=True, help="Output raw result without formatting")
@click.pass_context
def call_tool(
    ctx: click.Context,
    server_name: str,
    tool_name: str,
    args: tuple[str, ...],
    json_args: str | None,
    raw: bool,
) -> None:
    """Execute a tool on an MCP server.

    Examples:
        gobby mcp-proxy call-tool supabase list_tables
        gobby mcp-proxy call-tool context7 get-library-docs -a topic=react -a tokens=5000
        gobby mcp-proxy call-tool myserver mytool -j '{"key": "value"}'
    """
    client = get_daemon_client(ctx)
    if not check_daemon_running(client):
        sys.exit(1)

    # Parse arguments
    arguments: dict[str, Any] = {}

    if json_args:
        try:
            arguments = json.loads(json_args)
        except json.JSONDecodeError as e:
            click.echo(f"Error: Invalid JSON arguments: {e}", err=True)
            sys.exit(1)

    # Add key=value args (override JSON args)
    for arg in args:
        if "=" not in arg:
            click.echo(f"Error: Invalid argument format '{arg}'. Use key=value", err=True)
            sys.exit(1)
        key, value = arg.split("=", 1)
        # Try to parse value as JSON for proper typing
        try:
            arguments[key] = json.loads(value)
        except json.JSONDecodeError:
            arguments[key] = value

    result = call_mcp_api(
        client,
        "/api/mcp/tools/call",
        method="POST",
        json_data={
            "server_name": server_name,
            "tool_name": tool_name,
            "arguments": arguments,
        },
    )
    if result is None:
        sys.exit(1)

    if raw:
        click.echo(json_dumps(result, indent=2))
    else:
        # Format result nicely
        if result.get("success"):
            content = result.get("result", result)
            if isinstance(content, dict):
                click.echo(json_dumps(content, indent=2))
            else:
                click.echo(content)
        else:
            click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)
            sys.exit(1)


@mcp_proxy.command("add-server")
@click.argument("name")
@click.option("--transport", "-t", type=click.Choice(["http", "stdio", "websocket"]))
@click.option("--url", "-u", help="Server URL (for http/websocket)")
@click.option("--command", "-c", help="Command to run (for stdio)")
@click.option("--args", "-A", "cmd_args", help="Command arguments as JSON array (for stdio)")
@click.option("--env", "-e", help="Environment variables as JSON object")
@click.option("--headers", help="HTTP headers as JSON object")
@click.option("--disabled", is_flag=True, help="Add server as disabled")
@click.option("--template", help="Instantiate from a named MCP server template")
@click.option("--set", "value_sets", multiple=True, help="Template value as key=value")
@click.option("--description", help="Instance description")
@click.option("--global", "global_scope", is_flag=True, help="Create a machine-wide instance")
@click.pass_context
def add_server(
    ctx: click.Context,
    name: str,
    transport: str | None,
    url: str | None,
    command: str | None,
    cmd_args: str | None,
    env: str | None,
    headers: str | None,
    disabled: bool,
    template: str | None,
    value_sets: tuple[str, ...],
    description: str | None,
    global_scope: bool,
) -> None:
    """Add a new MCP server configuration.

    Examples:
        gobby mcp-proxy add-server my-http -t http -u https://api.example.com/mcp
        gobby mcp-proxy add-server my-stdio -t stdio -c npx --args '["mcp-server"]'
        gobby mcp-proxy add-server demo --template demo --set region=us
    """
    client = get_daemon_client(ctx)
    if not check_daemon_running(client):
        sys.exit(1)

    if not template and not transport:
        click.echo("Error: --transport is required without --template", err=True)
        sys.exit(1)
    if transport in ("http", "websocket") and not url:
        click.echo(f"Error: --url is required for {transport} transport", err=True)
        sys.exit(1)
    if transport == "stdio" and not command:
        click.echo("Error: --command is required for stdio transport", err=True)
        sys.exit(1)

    parsed_args = None
    parsed_env = None
    parsed_headers = None
    values: dict[str, str] = {}

    if cmd_args:
        try:
            parsed_args = json.loads(cmd_args)
        except json.JSONDecodeError as e:
            click.echo(f"Error: Invalid JSON for --args: {e}", err=True)
            sys.exit(1)

    if env:
        try:
            parsed_env = json.loads(env)
        except json.JSONDecodeError as e:
            click.echo(f"Error: Invalid JSON for --env: {e}", err=True)
            sys.exit(1)

    if headers:
        try:
            parsed_headers = json.loads(headers)
        except json.JSONDecodeError as e:
            click.echo(f"Error: Invalid JSON for --headers: {e}", err=True)
            sys.exit(1)

    for item in value_sets:
        if "=" not in item:
            click.echo(f"Error: Invalid --set '{item}'. Use key=value", err=True)
            sys.exit(1)
        key, raw = item.split("=", 1)
        values[key] = raw

    project_id, scope = resolve_cli_mcp_project(global_scope=global_scope)
    payload: dict[str, Any] = {
        "name": name,
        "transport": transport,
        "url": url,
        "command": command,
        "args": parsed_args,
        "env": parsed_env,
        "headers": parsed_headers,
        "enabled": not disabled,
        "scope": scope,
    }
    if project_id:
        payload["project_id"] = project_id
    if template:
        payload["template"] = template
        payload["values"] = values
    if description:
        payload["description"] = description

    result = call_mcp_api(client, "/api/mcp/servers", method="POST", json_data=payload)
    if result is None:
        sys.exit(1)

    if not result.get("success"):
        click.echo(f"Error: {result.get('error', 'Failed to add server')}", err=True)
        sys.exit(1)

    missing = list(result.get("missing_secrets") or [])
    if missing and _stdin_is_tty():
        for secret in missing:
            value = click.prompt(f"Secret {secret}", hide_input=True)
            store_cli_secret(secret, value, project_id=project_id, global_scope=global_scope)
        refresh_payload: dict[str, Any] = {"server": name, "scope": scope}
        if project_id:
            refresh_payload["project_id"] = project_id
        call_mcp_api(client, "/api/mcp/refresh", method="POST", json_data=refresh_payload)
        click.echo(f"Added MCP server: {name}")
        return
    if missing:
        click.echo("needs_configuration")
        for command in result.get("configure") or []:
            click.echo(command)
        sys.exit(0)

    click.echo(f"Added MCP server: {name}")


@mcp_proxy.command("remove-server")
@click.argument("name")
@click.confirmation_option(prompt="Are you sure you want to remove this server?")
@click.pass_context
def remove_server(ctx: click.Context, name: str) -> None:
    """Remove an MCP server configuration."""
    client = get_daemon_client(ctx)
    if not check_daemon_running(client):
        sys.exit(1)

    encoded_name = urllib.parse.quote(name, safe="")
    result = call_mcp_api(
        client,
        f"/api/mcp/servers/{encoded_name}",
        method="DELETE",
    )
    if result is None:
        sys.exit(1)

    if result.get("success"):
        click.echo(f"Removed MCP server: {name}")
    else:
        click.echo(f"Error: {result.get('error', 'Failed to remove server')}", err=True)
        sys.exit(1)


@mcp_proxy.command("recommend-tools")
@click.argument("task_description")
@click.option("--agent", "-a", "agent_id", help="Agent ID for filtered recommendations")
@click.option(
    "--mode",
    "-m",
    "search_mode",
    type=click.Choice(["llm", "semantic", "hybrid"]),
    default="llm",
    help="Search mode: llm (default), semantic, or hybrid",
)
@click.option("--top-k", "-k", type=int, default=10, help="Max results (semantic/hybrid)")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def recommend_tools(
    ctx: click.Context,
    task_description: str,
    agent_id: str | None,
    search_mode: str,
    top_k: int,
    json_format: bool,
) -> None:
    """Get AI-powered tool recommendations for a task.

    Examples:
        gobby mcp-proxy recommend-tools "I need to query a database"
        gobby mcp-proxy recommend-tools "Search for files" --mode semantic
        gobby mcp-proxy recommend-tools "Search for documentation" --agent my-agent
    """
    import os

    client = get_daemon_client(ctx)
    if not check_daemon_running(client):
        sys.exit(1)

    result = call_mcp_api(
        client,
        "/api/mcp/tools/recommend",
        method="POST",
        json_data={
            "task_description": task_description,
            "agent_id": agent_id,
            "search_mode": search_mode,
            "top_k": top_k,
            "cwd": os.getcwd(),
        },
        timeout=120.0,  # LLM/embedding generation can be slow
    )
    if result is None:
        sys.exit(1)

    if json_format:
        click.echo(json_dumps(result, indent=2))
        return

    recommendations = result.get("recommendations", [])
    if not recommendations:
        click.echo("No tool recommendations found.")
        return

    click.echo("Recommended tools:")
    for rec in recommendations:
        server = rec.get("server", "unknown")
        tool = rec.get("tool", "unknown")
        reason = rec.get("reason", "")
        click.echo(f"  • {server}/{tool}")
        if reason:
            click.echo(f"    {reason}")


@mcp_proxy.command("search-tools")
@click.argument("query")
@click.option("--top-k", "-k", type=int, default=10, help="Max results to return")
@click.option("--min-similarity", "-s", type=float, default=0.0, help="Min similarity threshold")
@click.option("--server", "-S", help="Filter by server name")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def search_tools(
    ctx: click.Context,
    query: str,
    top_k: int,
    min_similarity: float,
    server: str | None,
    json_format: bool,
) -> None:
    """Search for tools using semantic similarity.

    Examples:
        gobby mcp-proxy search-tools "query a database"
        gobby mcp-proxy search-tools "search files" --top-k 5
    """
    import os

    client = get_daemon_client(ctx)
    if not check_daemon_running(client):
        sys.exit(1)

    result = call_mcp_api(
        client,
        "/api/mcp/tools/search",
        method="POST",
        json_data={
            "query": query,
            "top_k": top_k,
            "min_similarity": min_similarity,
            "server": server,
            "cwd": os.getcwd(),
        },
        timeout=120.0,  # Embedding generation can be slow
    )
    if result is None:
        sys.exit(1)

    if json_format:
        click.echo(json_dumps(result, indent=2))
        return

    results = result.get("results", [])
    if not results:
        click.echo("No matching tools found.")
        return

    click.echo(f"Found {len(results)} tools matching '{query}':")
    for r in results:
        server_name = r.get("server_name", "unknown")
        tool_name = r.get("tool_name", "unknown")
        similarity = r.get("similarity", 0)
        desc = r.get("description", "")
        click.echo(f"  • {server_name}/{tool_name} (similarity: {similarity:.2%})")
        if desc:
            # Truncate long descriptions
            if len(desc) > 80:
                desc = desc[:77] + "..."
            click.echo(f"    {desc}")


@mcp_proxy.command("import-server")
@click.option("--from-project", "-p", help="Import from another Gobby project")
@click.option("--github", "-g", "github_url", help="Import from GitHub repository URL")
@click.option("--query", "-q", help="Search for MCP server by name/description")
@click.option("--server", "-s", "servers", multiple=True, help="Specific servers to import")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def import_server(
    ctx: click.Context,
    from_project: str | None,
    github_url: str | None,
    query: str | None,
    servers: tuple[str, ...],
    json_format: bool,
) -> None:
    """Import MCP server(s) from various sources.

    Examples:
        gobby mcp-proxy import-server --from-project my-other-project
        gobby mcp-proxy import-server --from-project prod -s context7 -s exa
        gobby mcp-proxy import-server --github https://github.com/user/mcp-server
        gobby mcp-proxy import-server --query "supabase mcp server"
    """
    client = get_daemon_client(ctx)
    if not check_daemon_running(client):
        sys.exit(1)

    # Validate that at least one source is specified
    if not from_project and not github_url and not query:
        click.echo(
            "Error: Specify at least one source: --from-project, --github, or --query",
            err=True,
        )
        sys.exit(1)

    result = call_mcp_api(
        client,
        "/api/mcp/servers/import",
        method="POST",
        json_data={
            "from_project": from_project,
            "github_url": github_url,
            "query": query,
            "servers": list(servers) if servers else None,
        },
    )
    if result is None:
        sys.exit(1)

    if json_format:
        click.echo(json_dumps(result, indent=2))
        return

    # Handle different result statuses
    if result.get("status") == "needs_configuration":
        click.echo("Server configuration extracted but needs secrets:")
        config = result.get("config", {})
        click.echo(f"  Name: {config.get('name')}")
        click.echo(f"  Transport: {config.get('transport')}")
        missing = result.get("missing", [])
        if missing:
            click.echo(f"  Missing secrets: {', '.join(missing)}")
        if result.get("instructions"):
            click.echo(f"\nInstructions:\n{result['instructions']}")
        click.echo("\nUse 'gobby mcp-proxy add-server' to add with required values.")
        return

    if result.get("status") == "requires_approval":
        click.echo("Server configuration extracted and ready for approval:")
        config = result.get("config", {})
        click.echo(f"  Name: {config.get('name')}")
        click.echo(f"  Transport: {config.get('transport')}")
        if result.get("instructions"):
            click.echo(f"\nInstructions:\n{result['instructions']}")
        click.echo("\nUse 'gobby mcp-proxy add-server' to add this server.")
        return

    if result.get("success"):
        imported = result.get("imported", [])
        if imported:
            click.echo(f"Imported {len(imported)} server(s):")
            for name in imported:
                click.echo(f"  + {name}")

        skipped = result.get("skipped", [])
        if skipped:
            click.echo(f"Skipped {len(skipped)} existing server(s):")
            for name in skipped:
                click.echo(f"  - {name}")

        failed = result.get("failed", [])
        if failed:
            click.echo(f"Failed to import {len(failed)} server(s):")
            for item in failed:
                click.echo(f"  x {item.get('name')}: {item.get('error')}")
    else:
        click.echo(f"Error: {result.get('error', 'Import failed')}", err=True)
        if result.get("available_projects"):
            click.echo(f"Available projects: {', '.join(result['available_projects'])}")
        sys.exit(1)


@mcp_proxy.command("refresh")
@click.option("--force", "-f", is_flag=True, help="Force full refresh, ignore cached hashes")
@click.option("--server", "-s", help="Only refresh a specific server")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def refresh_tools(ctx: click.Context, force: bool, server: str | None, json_format: bool) -> None:
    """Refresh MCP tools - detect schema changes and re-index.

    Scans all connected MCP servers for tool schema changes and regenerates
    embeddings for new or modified tools. Unchanged tools are skipped.

    Examples:
        gobby mcp-proxy refresh
        gobby mcp-proxy refresh --force
        gobby mcp-proxy refresh --server context7
    """
    import os

    client = get_daemon_client(ctx)
    if not check_daemon_running(client):
        sys.exit(1)

    json_data: dict[str, Any] = {
        "cwd": os.getcwd(),
        "force": force,
        "server": server,
    }
    project_id = _cwd_project_id()
    if project_id:
        json_data["project_id"] = project_id
    result = call_mcp_api(
        client,
        "/api/mcp/refresh",
        method="POST",
        json_data=json_data,
        timeout=300.0,  # Embedding generation can be slow
    )
    if result is None:
        sys.exit(1)

    if json_format:
        click.echo(json_dumps(result, indent=2))
        return

    if not result.get("success"):
        click.echo(f"Error: {result.get('error', 'Refresh failed')}", err=True)
        sys.exit(1)

    stats = result.get("stats", {})

    # Summary
    click.echo("MCP Tools Refresh Complete")
    click.echo(f"  Servers processed: {stats.get('servers_processed', 0)}")
    click.echo(f"  New tools: {stats.get('tools_new', 0)}")
    click.echo(f"  Changed tools: {stats.get('tools_changed', 0)}")
    click.echo(f"  Unchanged tools: {stats.get('tools_unchanged', 0)}")
    click.echo(f"  Removed tools: {stats.get('tools_removed', 0)}")
    click.echo(f"  Embeddings generated: {stats.get('embeddings_generated', 0)}")

    if force:
        click.echo("\n(--force: all tools treated as new)")

    # Per-server breakdown
    by_server = stats.get("by_server", {})
    if by_server:
        click.echo("\nBy Server:")
        for srv_name, srv_stats in by_server.items():
            if "error" in srv_stats:
                click.echo(f"  ✗ {srv_name}: {srv_stats['error']}")
            else:
                new = srv_stats.get("new", 0)
                changed = srv_stats.get("changed", 0)
                unchanged = srv_stats.get("unchanged", 0)
                click.echo(f"  ● {srv_name}: {new} new, {changed} changed, {unchanged} unchanged")


@mcp_proxy.command("status")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def proxy_status(ctx: click.Context, json_format: bool) -> None:
    """Show MCP proxy status and health."""
    client = get_daemon_client(ctx)
    if not check_daemon_running(client):
        sys.exit(1)

    result = call_mcp_api(client, "/api/mcp/status", method="GET")
    if result is None:
        sys.exit(1)

    if json_format:
        click.echo(json_dumps(result, indent=2))
        return

    click.echo("MCP Proxy Status:")
    click.echo(f"  Servers: {result.get('total_servers', 0)}")
    click.echo(f"  Connected: {result.get('connected_servers', 0)}")
    click.echo(f"  Tools cached: {result.get('cached_tools', 0)}")

    health = result.get("server_health", {})
    if health:
        click.echo("\nServer Health:")
        for name, info in health.items():
            state = info.get("state", "unknown")
            health_status = info.get("health", "unknown")
            failures = info.get("failures", 0)
            icon = "●" if state == "connected" else "○"
            click.echo(f"  {icon} {name}: {state} ({health_status})", nl=False)
            if failures > 0:
                click.echo(f" - {failures} failures", nl=False)
            click.echo()


def register_template_commands() -> None:
    """Load template subcommands after the Click group exists."""
    import gobby.cli.mcp_proxy_templates as template_commands

    _ = (template_commands.list_templates, template_commands.show_template)


register_template_commands()
