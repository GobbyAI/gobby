"""
Communications CLI commands.

Commands for managing communications channels and sending messages.
"""

import json
from typing import Any

import click
import httpx

from gobby.cli.mcp_proxy import get_daemon_client


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
        response = client.call_http_api("/api/comms/channels?status=true", method="GET")
        if response.status_code != 200:
            print_error(f"Failed to fetch channel status: {response.text}")
            ctx.exit(1)

        channels = response.json().get("channels", [])
        if not channels:
            click.echo("No communications channels configured.")
            return

        click.echo("\nChannel Status")
        click.echo("=" * 40)

        table_data = []
        for ch in channels:
            status = ch.get("status", "unknown")
            color = "green" if status == "connected" else "red" if status == "error" else "yellow"
            status_text = click.style(status, fg=color)

            table_data.append(
                {
                    "Name": ch.get("name", ""),
                    "Type": ch.get("channel_type", ""),
                    "Enabled": "Yes" if ch.get("enabled") else "No",
                    "Status": status_text,
                    "Messages (In/Out)": f"{ch.get('stats', {}).get('inbound', 0)} / {ch.get('stats', {}).get('outbound', 0)}",
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

        channels = response.json().get("channels", [])
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
        config["channel_id"] = click.prompt("Channel ID (optional)", default="")
    elif channel_type == "telegram":
        secrets["bot_token"] = click.prompt("Bot Token", hide_input=True)
        config["chat_id"] = click.prompt("Chat ID (optional)", default="")
    elif channel_type == "discord":
        secrets["bot_token"] = click.prompt("Bot Token", hide_input=True)
        config["channel_id"] = click.prompt("Channel ID (optional)", default="")
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

        channels = channels_resp.json().get("channels", [])
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


@channels_group.command(name="list-default", hidden=True)
@click.pass_context
def _channels_list_default(ctx: click.Context) -> None:
    ctx.invoke(channels_list_cmd)
