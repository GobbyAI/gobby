"""
Communications CLI commands.

Commands for managing communications channels and sending messages.
"""

import json
import urllib.parse
from typing import Any

import click
import httpx

from gobby.cli.mcp_proxy import get_daemon_client
from gobby.cli.utils_resolution import resolve_project_ref


def print_error(msg: str) -> None:
    click.secho(f"Error: {msg}", fg="red")


def print_success(msg: str) -> None:
    click.secho(msg, fg="green")


def print_table(data: list[dict[str, Any]]) -> None:
    if not data:
        return

    keys = list(data[0].keys())
    widths = {
        k: max(len(str(k)), max((len(str(row.get(k, ""))) for row in data), default=0))
        for k in keys
    }

    header = " | ".join(str(k).ljust(widths[k]) for k in keys)
    click.echo(header)
    click.echo("-" * len(header))

    for row in data:
        line = " | ".join(str(row.get(k, "")).ljust(widths[k]) for k in keys)
        click.echo(line)


@click.group(name="comms")
def comms() -> None:
    """Manage communications channels and messages."""
    pass


@comms.command(name="status")
@click.pass_context
def status_cmd(ctx: click.Context) -> None:
    """Show status of enabled channels and message counts."""
    client = get_daemon_client(ctx)

    try:
        response = client.call_http_api("/api/comms/channels", method="GET")
        if response.status_code != 200:
            print_error(f"Failed to fetch channel status: {response.text}")
            ctx.exit(1)

        channels = response.json()
        if not channels:
            click.echo("No communications channels configured.")
            return

        click.echo("\nChannel Status")
        click.echo("=" * 40)

        table_data = []
        for ch in channels:
            status = (
                "error" if ch.get("init_error") else "active" if ch.get("active") else "inactive"
            )
            color = "green" if status == "active" else "red" if status == "error" else "yellow"
            status_text = click.style(status, fg=color)

            table_data.append(
                {
                    "Name": ch.get("name", ""),
                    "Type": ch.get("channel_type", ""),
                    "Enabled": "Yes" if ch.get("enabled") else "No",
                    "Status": status_text,
                }
            )

        print_table(table_data)

    except httpx.RequestError as e:
        print_error(f"Daemon connection failed: {e}")
        ctx.exit(1)


@comms.command(name="send")
@click.argument("channel_name")
@click.argument("message")
@click.pass_context
def send_cmd(ctx: click.Context, channel_name: str, message: str) -> None:
    """Send a message to a specific channel."""
    client = get_daemon_client(ctx)

    try:
        response = client.call_http_api(
            "/api/comms/send",
            method="POST",
            json_data={"channel_name": channel_name, "content": message},
        )
        if response.status_code == 200:
            print_success(f"Message sent to {channel_name}")
        else:
            print_error(f"Failed to send message: {response.text}")
            ctx.exit(1)

    except httpx.RequestError as e:
        print_error(f"Daemon connection failed: {e}")
        ctx.exit(1)


@comms.group(name="channels")
def channels_group() -> None:
    """Manage communication channels."""
    pass


@channels_group.command(name="list")
@click.pass_context
def channels_list_cmd(ctx: click.Context) -> None:
    """List all configured communication channels."""
    client = get_daemon_client(ctx)

    try:
        response = client.call_http_api("/api/comms/channels", method="GET")
        if response.status_code != 200:
            print_error(f"Failed to fetch channels: {response.text}")
            ctx.exit(1)

        channels = response.json()
        if not channels:
            click.echo("No communications channels configured.")
            return

        table_data = []
        for ch in channels:
            table_data.append(
                {
                    "ID": ch.get("id", ""),
                    "Name": ch.get("name", ""),
                    "Type": ch.get("channel_type", ""),
                    "Enabled": "Yes" if ch.get("enabled") else "No",
                }
            )

        print_table(table_data)

    except httpx.RequestError as e:
        print_error(f"Daemon connection failed: {e}")
        ctx.exit(1)


@channels_group.command(name="add")
@click.argument("channel_type")
@click.argument("name")
@click.pass_context
def channels_add_cmd(ctx: click.Context, channel_type: str, name: str) -> None:
    """Add a new communication channel.

    You will be prompted for type-specific configuration.
    """
    client = get_daemon_client(ctx)
    config: dict[str, Any] = {}
    secrets: dict[str, Any] = {}

    click.echo(f"Configuring {channel_type} channel: {name}")

    if channel_type == "slack":
        secrets["bot_token"] = click.prompt("Bot Token", hide_input=True)
        secrets["signing_secret"] = click.prompt("Signing Secret", hide_input=True)
        config["default_destination"] = click.prompt("Channel ID (optional)", default="")
    elif channel_type == "telegram":
        secrets["bot_token"] = click.prompt("Bot Token", hide_input=True)
        config["default_destination"] = click.prompt("Chat ID (optional)", default="")
    elif channel_type == "discord":
        secrets["bot_token"] = click.prompt("Bot Token", hide_input=True)
        config["default_destination"] = click.prompt("Channel ID (optional)", default="")
    elif channel_type == "teams":
        secrets["app_id"] = click.prompt("App ID", hide_input=True)
        secrets["app_password"] = click.prompt("App Password", hide_input=True)
    elif channel_type == "email":
        secrets["password"] = click.prompt("Password", hide_input=True)
        config["smtp_host"] = click.prompt("SMTP Host")
        config["smtp_port"] = click.prompt("SMTP Port", type=int)
        config["imap_host"] = click.prompt("IMAP Host")
        config["imap_port"] = click.prompt("IMAP Port", type=int)
        config["from_address"] = click.prompt("From Address")
    elif channel_type == "sms":
        secrets["auth_token"] = click.prompt("Auth Token", hide_input=True)
        config["account_sid"] = click.prompt("Account SID")
        config["from_number"] = click.prompt("From Number")
        config["webhook_url"] = click.prompt(
            "Webhook URL (for signature verification, optional)", default=""
        )
    elif channel_type == "gobby_chat":
        click.echo("No additional configuration required.")
    else:
        click.echo("Enter raw JSON configuration for this channel type:")
        config_str = click.prompt("Config JSON", default="{}")
        try:
            config = json.loads(config_str)
        except json.JSONDecodeError:
            print_error("Invalid JSON configuration.")
            ctx.exit(1)
        if not isinstance(config, dict):
            print_error("Configuration must be a JSON object.")
            ctx.exit(1)

    # Remove empty optional values
    config = {k: v for k, v in config.items() if v != ""}
    secrets = {k: v for k, v in secrets.items() if v}

    try:
        response = client.call_http_api(
            "/api/comms/channels",
            method="POST",
            json_data={
                "name": name,
                "channel_type": channel_type,
                "config": config,
                "secrets": secrets if secrets else None,
            },
        )
        if response.status_code in (200, 201):
            print_success(f"Channel '{name}' added successfully.")
        else:
            print_error(f"Failed to add channel: {response.text}")
            ctx.exit(1)

    except httpx.RequestError as e:
        print_error(f"Daemon connection failed: {e}")
        ctx.exit(1)


@channels_group.command(name="remove")
@click.argument("name")
@click.confirmation_option(prompt="Are you sure you want to remove this channel?")
@click.pass_context
def channels_remove_cmd(ctx: click.Context, name: str) -> None:
    """Remove a communication channel by name."""
    client = get_daemon_client(ctx)

    try:
        channels_resp = client.call_http_api("/api/comms/channels", method="GET")
        if channels_resp.status_code != 200:
            print_error("Failed to fetch channels to find ID.")
            ctx.exit(1)

        channels = channels_resp.json()
        channel_id = next((ch["id"] for ch in channels if ch["name"] == name), None)

        if not channel_id:
            print_error(f"Channel '{name}' not found.")
            ctx.exit(1)

        response = client.call_http_api(f"/api/comms/channels/{channel_id}", method="DELETE")
        if response.status_code in (200, 204):
            print_success(f"Channel '{name}' removed successfully.")
        else:
            print_error(f"Failed to remove channel: {response.text}")
            ctx.exit(1)

    except httpx.RequestError as e:
        print_error(f"Daemon connection failed: {e}")
        ctx.exit(1)


@comms.group("subscriptions")
def subscriptions_group() -> None:
    """Manage event subscriptions."""


@subscriptions_group.command("create")
@click.argument("name")
@click.option("--channel", required=True, help="Channel UUID or exact name.")
@click.option("--event", "event_pattern", required=True, help="Event name or glob.")
@click.option("--project", "project_ref", help="Project UUID or exact name.")
@click.option("--global", "global_scope", is_flag=True)
@click.option("--session", "session_id", help="Optional session UUID.")
@click.option("--priority", type=int, default=0, show_default=True)
@click.option("--disabled", is_flag=True)
@click.pass_context
def subscriptions_create_cmd(
    ctx: click.Context,
    name: str,
    channel: str,
    event_pattern: str,
    project_ref: str | None,
    global_scope: bool,
    session_id: str | None,
    priority: int,
    disabled: bool,
) -> None:
    """Create an event subscription."""
    if global_scope and project_ref:
        print_error("Choose either --project or --global.")
        ctx.exit(1)
    project_id = None if global_scope else resolve_project_ref(project_ref)
    if not global_scope and project_id is None:
        print_error("No project context found; pass --project or --global.")
        ctx.exit(1)

    client = get_daemon_client(ctx)
    try:
        response = client.call_http_api(
            "/api/comms/subscriptions",
            method="POST",
            json_data={
                "name": name,
                "channel": channel,
                "event_pattern": event_pattern,
                "project_id": project_id,
                "global_scope": global_scope,
                "session_id": session_id,
                "priority": priority,
                "enabled": not disabled,
            },
        )
        if response.status_code not in (200, 201):
            print_error(f"Failed to create subscription: {response.text}")
            ctx.exit(1)
        click.echo(json.dumps(response.json(), indent=2))
    except httpx.RequestError as e:
        print_error(f"Daemon connection failed: {e}")
        ctx.exit(1)


@subscriptions_group.command("list")
@click.option("--channel")
@click.option("--project", "project_ref")
@click.option("--global", "global_scope", is_flag=True, default=False)
@click.option("--enabled", "enabled_filter", flag_value=True, default=None)
@click.option("--disabled", "enabled_filter", flag_value=False)
@click.option("--event", "event_pattern")
@click.pass_context
def subscriptions_list_cmd(
    ctx: click.Context,
    channel: str | None,
    project_ref: str | None,
    global_scope: bool,
    enabled_filter: bool | None,
    event_pattern: str | None,
) -> None:
    """List event subscriptions."""
    if global_scope and project_ref:
        print_error("Choose either --project or --global.")
        ctx.exit(1)
    params: dict[str, Any] = {}
    if channel is not None:
        params["channel"] = channel
    if project_ref is not None:
        params["project_id"] = resolve_project_ref(project_ref)
    if global_scope:
        params["global_scope"] = True
    if enabled_filter is not None:
        params["enabled"] = enabled_filter
    if event_pattern is not None:
        params["event_pattern"] = event_pattern

    client = get_daemon_client(ctx)
    try:
        query = urllib.parse.urlencode(params)
        endpoint = f"/api/comms/subscriptions?{query}" if query else "/api/comms/subscriptions"
        response = client.call_http_api(
            endpoint,
            method="GET",
        )
        if response.status_code != 200:
            print_error(f"Failed to list subscriptions: {response.text}")
            ctx.exit(1)
        click.echo(json.dumps(response.json(), indent=2))
    except httpx.RequestError as e:
        print_error(f"Daemon connection failed: {e}")
        ctx.exit(1)


@subscriptions_group.command("get")
@click.argument("subscription_id")
@click.pass_context
def subscriptions_get_cmd(ctx: click.Context, subscription_id: str) -> None:
    """Get one event subscription."""
    client = get_daemon_client(ctx)
    try:
        response = client.call_http_api(
            f"/api/comms/subscriptions/{subscription_id}",
            method="GET",
        )
        if response.status_code != 200:
            print_error(f"Failed to get subscription: {response.text}")
            ctx.exit(1)
        click.echo(json.dumps(response.json(), indent=2))
    except httpx.RequestError as e:
        print_error(f"Daemon connection failed: {e}")
        ctx.exit(1)


@subscriptions_group.command("update")
@click.argument("subscription_id")
@click.option("--name")
@click.option("--channel")
@click.option("--event", "event_pattern")
@click.option("--project", "project_ref")
@click.option("--global", "global_scope", is_flag=True, default=False)
@click.option("--session", "session_id")
@click.option("--clear-session", is_flag=True)
@click.option("--priority", type=int)
@click.option("--enabled", "enabled_value", flag_value=True, default=None)
@click.option("--disabled", "enabled_value", flag_value=False)
@click.pass_context
def subscriptions_update_cmd(
    ctx: click.Context,
    subscription_id: str,
    name: str | None,
    channel: str | None,
    event_pattern: str | None,
    project_ref: str | None,
    global_scope: bool,
    session_id: str | None,
    clear_session: bool,
    priority: int | None,
    enabled_value: bool | None,
) -> None:
    """Partially update an event subscription."""
    if global_scope and project_ref:
        print_error("Choose either --project or --global.")
        ctx.exit(1)
    if session_id and clear_session:
        print_error("Choose either --session or --clear-session.")
        ctx.exit(1)

    changes: dict[str, Any] = {}
    for key, value in (
        ("name", name),
        ("channel", channel),
        ("event_pattern", event_pattern),
        ("priority", priority),
        ("enabled", enabled_value),
    ):
        if value is not None:
            changes[key] = value
    if project_ref is not None:
        changes["project_id"] = resolve_project_ref(project_ref)
        changes["global_scope"] = False
    elif global_scope:
        changes["global_scope"] = True
    if session_id is not None or clear_session:
        changes["session_id"] = session_id
    if not changes:
        print_error("No updates specified.")
        ctx.exit(1)

    client = get_daemon_client(ctx)
    try:
        response = client.call_http_api(
            f"/api/comms/subscriptions/{subscription_id}",
            method="PATCH",
            json_data=changes,
        )
        if response.status_code != 200:
            print_error(f"Failed to update subscription: {response.text}")
            ctx.exit(1)
        click.echo(json.dumps(response.json(), indent=2))
    except httpx.RequestError as e:
        print_error(f"Daemon connection failed: {e}")
        ctx.exit(1)


@subscriptions_group.command("delete")
@click.argument("subscription_id")
@click.pass_context
def subscriptions_delete_cmd(ctx: click.Context, subscription_id: str) -> None:
    """Delete an event subscription."""
    client = get_daemon_client(ctx)
    try:
        response = client.call_http_api(
            f"/api/comms/subscriptions/{subscription_id}",
            method="DELETE",
        )
        if response.status_code not in (200, 204):
            print_error(f"Failed to delete subscription: {response.text}")
            ctx.exit(1)
        click.echo(json.dumps(response.json(), indent=2))
    except httpx.RequestError as e:
        print_error(f"Daemon connection failed: {e}")
        ctx.exit(1)


@channels_group.command(name="list-default", hidden=True)
@click.pass_context
def _channels_list_default(ctx: click.Context) -> None:
    ctx.invoke(channels_list_cmd)
