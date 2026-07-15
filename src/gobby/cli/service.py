"""
CLI commands for OS-level service management.

Provides `gobby service install/uninstall/status/enable/disable` to manage
the daemon as a launchd (macOS) or systemd (Linux) service.
"""

import sys

import click

from .installers.service import (
    disable_service,
    enable_service,
    get_service_status,
    install_service,
    uninstall_service,
)


@click.group()
def service() -> None:
    """Manage the Gobby daemon as an OS-level service."""


@service.command()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose daemon logging in the service")
def install(verbose: bool) -> None:
    """Install Gobby as a system service for auto-start on boot."""
    result = install_service(verbose=verbose)

    if not result["success"]:
        click.echo(f"Failed: {result['error']}", err=True)
        sys.exit(1)

    platform = result.get("platform", "unknown")
    mode = result.get("mode", "unknown")
    click.echo(f"Service installed ({platform})")
    click.echo(f"  Mode: {mode}", nl=False)
    if mode == "dev":
        click.echo(f" (source: {result.get('working_directory', '?')})")
    else:
        click.echo()

    click.echo(f"  Python: {result.get('python_executable', '?')}")
    click.echo(f"  Logs: {result.get('log_file', '?')}")

    if result.get("plist_file"):
        click.echo(f"  Plist: {result['plist_file']}")
    if result.get("unit_file"):
        click.echo(f"  Unit: {result['unit_file']}")

    if result.get("cli_note") and result.get("mode") == "dev":
        click.echo(f"  CLI: {result['cli_note']}")

    if result.get("warnings"):
        click.echo("")
        for warning in result["warnings"]:
            click.echo(f"  Warning: {warning}")

    click.echo("")
    click.echo("The daemon will now start automatically on boot.")
    click.echo("")

    # Upgrade instructions
    if mode == "dev":
        click.echo("To upgrade: git pull && uv sync && uv run gobby service install")
    else:
        click.echo("To upgrade: uv tool upgrade gobby && gobby service install")

    click.echo("")
    click.echo("Note: API keys from your shell profile are NOT available to the service.")
    click.echo("Set them in ~/.gobby/bootstrap.yaml or use `gobby secrets set` instead.")


@service.command()
@click.confirmation_option(prompt="Remove OS service configuration?")
def uninstall() -> None:
    """Remove the Gobby system service."""
    result = uninstall_service()

    if not result["success"]:
        click.echo(f"Failed: {result['error']}", err=True)
        sys.exit(1)

    click.echo(f"Service uninstalled ({result.get('platform', 'unknown')})")
    click.echo("The daemon will no longer start on boot.")
    click.echo("Use `gobby start` to run it manually.")


@service.command()
def status() -> None:
    """Show OS service status."""
    result = get_service_status()

    platform = result.get("platform", "unknown")
    installed = result.get("installed", False)

    if not installed:
        click.echo(f"Service: not installed ({platform})")
        click.echo("Run `gobby service install` to set up auto-start on boot.")
        return

    enabled = result.get("enabled", False)
    running = result.get("running", False)
    mode = result.get("mode", "unknown")

    state_parts = []
    if running:
        state_parts.append("running")
    elif enabled:
        state_parts.append("enabled, not running")
    else:
        state_parts.append("disabled")

    state_parts.append(platform)
    state_parts.append(f"{mode} mode")

    click.echo(f"Service: installed ({', '.join(state_parts)})")

    if result.get("pid"):
        click.echo(f"  PID: {result['pid']}")
    if result.get("plist_file"):
        click.echo(f"  Plist: {result['plist_file']}")
    if result.get("unit_file"):
        click.echo(f"  Unit: {result['unit_file']}")

    if result.get("warnings"):
        click.echo("")
        for warning in result["warnings"]:
            click.echo(f"  Warning: {warning}")


@service.command()
def enable() -> None:
    """Re-enable the service after it was disabled."""
    result = enable_service()

    if not result["success"]:
        click.echo(f"Failed: {result['error']}", err=True)
        sys.exit(1)

    click.echo(f"Service enabled ({result.get('platform', 'unknown')})")


@service.command()
def disable() -> None:
    """Temporarily stop the service without uninstalling.

    Use this for manual debug sessions with `gobby start --verbose`.
    Re-enable with `gobby service enable`.
    """
    result = disable_service()

    if not result["success"]:
        click.echo(f"Failed: {result['error']}", err=True)
        sys.exit(1)

    click.echo(f"Service disabled ({result.get('platform', 'unknown')})")
    click.echo("The daemon is stopped but the service file remains.")
    click.echo("Use `gobby start --verbose` for a manual debug session.")
    click.echo("Re-enable with `gobby service enable`.")
