"""
CLI commands for hook extensions (hooks, plugins, webhooks).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import TYPE_CHECKING

import click

from gobby.cli.mcp_proxy import call_mcp_api, check_daemon_running, get_daemon_client

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from gobby.hooks.events import HookEventType

# =============================================================================
# Hooks Commands
# =============================================================================


@click.group()
def hooks() -> None:
    """Manage hook system configuration and testing."""


@hooks.command("list")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def hooks_list(ctx: click.Context, json_format: bool) -> None:
    """List supported hook event types."""
    from gobby.hooks.events import HookEventType

    hook_types = [{"name": e.value, "description": _get_hook_description(e)} for e in HookEventType]

    if json_format:
        click.echo(json.dumps(hook_types, indent=2))
        return

    click.echo("Supported Hook Event Types:")
    click.echo()
    for hook in hook_types:
        click.echo(f"  {hook['name']}")
        if hook["description"]:
            click.echo(f"    {hook['description']}")


def _get_hook_description(event_type: HookEventType) -> str:
    """Get description for a hook event type."""
    from gobby.hooks.events import HookEventType

    descriptions = {
        HookEventType.SESSION_START: "Fired when a new session starts",
        HookEventType.SESSION_END: "Fired when a session ends",
        HookEventType.BEFORE_AGENT: "Fired before agent turn starts",
        HookEventType.AFTER_AGENT: "Fired after agent turn completes",
        HookEventType.STOP: "Fired when agent attempts to stop (can block)",
        HookEventType.BEFORE_TOOL: "Fired before a tool is executed (can block)",
        HookEventType.AFTER_TOOL: "Fired after a tool completes",
        HookEventType.BEFORE_TOOL_SELECTION: "Fired before tool selection (Gemini)",
        HookEventType.BEFORE_MODEL: "Fired before model call (Gemini)",
        HookEventType.AFTER_MODEL: "Fired after model call (Gemini)",
        HookEventType.PRE_COMPACT: "Fired before session context is compacted",
        HookEventType.NOTIFICATION: "Notification event from CLI",
    }
    return descriptions.get(event_type, "")


@hooks.command("test")
@click.argument("hook_type")
@click.option(
    "--source",
    "-s",
    type=click.Choice(["claude", "gemini", "codex"]),
    default="claude",
    help="Source CLI to simulate",
)
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def hooks_test(ctx: click.Context, hook_type: str, source: str, json_format: bool) -> None:
    """Test a hook by sending a test event to the daemon.

    HOOK_TYPE is the event type to test (e.g., session-start, before-tool).
    """
    client = get_daemon_client(ctx)
    if not check_daemon_running(client):
        sys.exit(1)

    # Build test payload
    test_payload = {
        "hook_type": hook_type,
        "source": source,
        "input_data": {
            "session_id": "test-session-cli",
            "tool_name": "test_tool" if "tool" in hook_type.lower() else None,
        },
    }

    result = call_mcp_api(
        client,
        "/api/hooks/execute",
        method="POST",
        json_data=test_payload,
    )

    if result is None:
        click.echo("Failed to execute test hook", err=True)
        sys.exit(1)

    if json_format:
        click.echo(json.dumps(result, indent=2))
        return

    click.echo(f"Hook test: {hook_type}")
    click.echo(f"  Source: {source}")
    click.echo(f"  Continue: {result.get('continue', 'unknown')}")
    if result.get("reason"):
        click.echo(f"  Reason: {result.get('reason')}")
    inject_context = result.get("inject_context")
    if inject_context:
        click.echo(f"  Context: {str(inject_context)[:100]}...")


@hooks.command("run")
@click.argument(
    "stage",
    type=click.Choice(["pre-commit", "pre-push", "pre-merge"]),
)
@click.option("--verbose", "-v", is_flag=True, help="Show command output")
@click.option("--dry-run", is_flag=True, help="Show what would run without executing")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def hooks_run(stage: str, verbose: bool, dry_run: bool, json_format: bool) -> None:
    """Run verification commands for a git hook stage.

    STAGE is the hook stage to run (pre-commit, pre-push, or pre-merge).

    This command reads verification commands from .gobby/project.json
    and executes them according to the hooks configuration.

    Example configuration in project.json:

    \b
      "verification": {
        "lint": "ruff check src/",
        "format": "ruff format --check src/"
      },
      "hooks": {
        "pre-commit": {
          "run": ["lint", "format"],
          "fail_fast": true
        }
      }
    """
    from gobby.hooks.verification_runner import VerificationRunner

    runner = VerificationRunner.from_project()

    # Handle dry-run mode
    if dry_run:
        stage_config = runner.get_stage_config(stage)
        if not stage_config or not stage_config.run:
            click.echo(f"No commands configured for '{stage}'")
            return

        if not runner.verification_config:
            click.echo("No verification commands defined in project.json")
            return

        click.echo(f"Would run for '{stage}':")
        for cmd_name in stage_config.run:
            command = runner.verification_config.get_command(cmd_name)
            if command:
                click.echo(f"  {cmd_name}: {command}")
            else:
                click.echo(f"  {cmd_name}: (not defined)")
        return

    # Run the stage
    result = runner.run_stage(stage)

    # Output as JSON
    if json_format:
        output = {
            "stage": result.stage,
            "success": result.success,
            "skipped": result.skipped,
            "skip_reason": result.skip_reason,
            "results": [
                {
                    "name": r.name,
                    "command": r.command,
                    "success": r.success,
                    "exit_code": r.exit_code,
                    "duration_ms": r.duration_ms,
                    "skipped": r.skipped,
                    "skip_reason": r.skip_reason,
                    "error": r.error,
                    "stdout": r.stdout if verbose else None,
                    "stderr": r.stderr if verbose else None,
                }
                for r in result.results
            ],
        }
        click.echo(json.dumps(output, indent=2))
        sys.exit(0 if result.success else 1)

    # Handle skipped stage
    if result.skipped:
        if verbose:
            click.echo(f"Skipped: {result.skip_reason}")
        sys.exit(0)

    # Display results
    for r in result.results:
        if r.skipped:
            click.echo(click.style(f"⊘ {r.name}: skipped", fg="yellow"))
            if r.skip_reason:
                click.echo(f"    {r.skip_reason}")
        elif r.success:
            click.echo(click.style(f"✓ {r.name}", fg="green") + f" ({r.duration_ms}ms)")
        else:
            click.echo(click.style(f"✗ {r.name}", fg="red") + f" ({r.duration_ms}ms)")
            if r.error:
                click.echo(f"    Error: {r.error}")
            if verbose and r.stderr:
                click.echo(f"    stderr:\n{_indent(r.stderr, 6)}")
            elif r.stderr:
                # Show first line of stderr even without verbose
                first_line = r.stderr.strip().split("\n")[0]
                if first_line:
                    click.echo(f"    {first_line}")

        if verbose and r.stdout:
            click.echo(f"    stdout:\n{_indent(r.stdout, 6)}")

    # Summary
    if result.results:
        click.echo()
        click.echo(
            f"Passed: {result.passed_count}, "
            f"Failed: {result.failed_count}, "
            f"Skipped: {result.skipped_count}"
        )

    sys.exit(0 if result.success else 1)


def _indent(text: str, spaces: int) -> str:
    """Indent each line of text by the specified number of spaces."""
    prefix = " " * spaces
    return "\n".join(prefix + line for line in text.strip().split("\n"))


@hooks.command("status")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def hooks_status(json_format: bool) -> None:
    """Show current hooks configuration from project.json.

    Displays global install status, per-project disable status,
    and which verification commands are configured for git hook stages.
    """
    from pathlib import Path

    from gobby.utils.project_context import get_hooks_config, get_verification_config

    verification_config = get_verification_config()
    hooks_config = get_hooks_config()

    # Check global install status
    global_hooks_dir = Path.home() / ".gobby" / "hooks"
    global_dispatcher = global_hooks_dir / "hook_dispatcher.py"
    global_installed = global_dispatcher.exists()

    # Check per-project disable status
    project_json_path = Path.cwd() / ".gobby" / "project.json"
    hooks_disabled = False
    if project_json_path.exists():
        try:
            with open(project_json_path) as f:
                project_config = json.load(f)
            hooks_disabled = project_config.get("hooks_disabled", False)
        except (json.JSONDecodeError, OSError) as e:
            logger.debug(f"Failed to read project config {project_json_path}: {e}")

    # Check env var
    env_disabled = bool(os.environ.get("GOBBY_HOOKS_DISABLED"))

    if json_format:
        output = {
            "global_installed": global_installed,
            "hooks_disabled": hooks_disabled,
            "env_disabled": env_disabled,
            "verification": verification_config.all_commands() if verification_config else {},
            "hooks": {
                "pre-commit": (
                    hooks_config.pre_commit.model_dump(by_alias=True) if hooks_config else None
                ),
                "pre-push": (
                    hooks_config.pre_push.model_dump(by_alias=True) if hooks_config else None
                ),
                "pre-merge": (
                    hooks_config.pre_merge.model_dump(by_alias=True) if hooks_config else None
                ),
            },
        }
        click.echo(json.dumps(output, indent=2))
        return

    # Display global install status
    click.echo("Global Hooks:")
    if global_installed:
        click.echo(f"  Installed: {global_hooks_dir}")
    else:
        click.echo("  Not installed (run 'gobby install')")

    # Display per-project status
    click.echo()
    click.echo("Per-Project Status:")
    if hooks_disabled:
        click.echo("  Hooks: DISABLED (hooks_disabled in .gobby/project.json)")
    elif env_disabled:
        click.echo("  Hooks: DISABLED (GOBBY_HOOKS_DISABLED env var set)")
    else:
        click.echo("  Hooks: enabled")

    click.echo()

    # Display verification commands
    click.echo("Verification Commands:")
    if verification_config:
        commands = verification_config.all_commands()
        if commands:
            for name, cmd in commands.items():
                click.echo(f"  {name}: {cmd}")
        else:
            click.echo("  (none configured)")
    else:
        click.echo("  (none configured)")

    click.echo()

    # Display hooks configuration
    click.echo("Hook Stages:")
    if not hooks_config:
        click.echo("  (none configured)")
        return

    for stage_name, stage_attr in [
        ("pre-commit", "pre_commit"),
        ("pre-push", "pre_push"),
        ("pre-merge", "pre_merge"),
    ]:
        stage_config = getattr(hooks_config, stage_attr)
        if stage_config.run:
            status = "enabled" if stage_config.enabled else "disabled"
            click.echo(f"  {stage_name} ({status}):")
            click.echo(f"    run: {', '.join(stage_config.run)}")
            click.echo(f"    fail_fast: {stage_config.fail_fast}")
            click.echo(f"    timeout: {stage_config.timeout}s")
        else:
            click.echo(f"  {stage_name}: (no commands)")


@hooks.command("disable")
def hooks_disable() -> None:
    """Disable hooks for the current project.

    Sets hooks_disabled: true in .gobby/project.json. Global hooks
    will pass through silently for this project.
    """
    from pathlib import Path

    project_json_path = Path.cwd() / ".gobby" / "project.json"

    if not project_json_path.exists():
        click.echo("No .gobby/project.json found in current directory.", err=True)
        click.echo("Run 'gobby init' first to initialize this project.")
        sys.exit(1)

    try:
        with open(project_json_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        click.echo(f"Failed to read project.json: {e}", err=True)
        sys.exit(1)

    config["hooks_disabled"] = True

    try:
        with open(project_json_path, "w") as f:
            json.dump(config, f, indent=2)
    except OSError as e:
        click.echo(f"Failed to write project.json: {e}", err=True)
        sys.exit(1)

    click.echo("Hooks disabled for this project.")
    click.echo("Hooks will pass through silently until re-enabled.")


@hooks.command("enable")
def hooks_enable() -> None:
    """Re-enable hooks for the current project.

    Removes hooks_disabled from .gobby/project.json.
    """
    from pathlib import Path

    project_json_path = Path.cwd() / ".gobby" / "project.json"

    if not project_json_path.exists():
        click.echo("No .gobby/project.json found in current directory.", err=True)
        click.echo("Run 'gobby init' first to initialize this project.")
        sys.exit(1)

    try:
        with open(project_json_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        click.echo(f"Failed to read project.json: {e}", err=True)
        sys.exit(1)

    if "hooks_disabled" in config:
        del config["hooks_disabled"]

    try:
        with open(project_json_path, "w") as f:
            json.dump(config, f, indent=2)
    except OSError as e:
        click.echo(f"Failed to write project.json: {e}", err=True)
        sys.exit(1)

    click.echo("Hooks re-enabled for this project.")


# =============================================================================
# Webhooks Commands
# =============================================================================


@click.group()
def webhooks() -> None:
    """Manage webhook endpoints."""


@webhooks.command("list")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def webhooks_list(ctx: click.Context, json_format: bool) -> None:
    """List configured webhook endpoints."""
    client = get_daemon_client(ctx)
    if not check_daemon_running(client):
        sys.exit(1)

    result = call_mcp_api(client, "/api/webhooks")

    if result is None:
        click.echo("Failed to list webhooks", err=True)
        sys.exit(1)

    if json_format:
        click.echo(json.dumps(result, indent=2))
        return

    enabled = result.get("enabled", False)
    endpoints = result.get("endpoints", [])

    if not enabled:
        click.echo("Webhook system is disabled in configuration.")
        click.echo("Enable with: hook_extensions.webhooks.enabled: true")
        return

    if not endpoints:
        click.echo("No webhook endpoints configured.")
        click.echo()
        click.echo("Configure webhooks via gobby-config MCP tools or the web UI:")
        click.echo("  hook_extensions:")
        click.echo("    webhooks:")
        click.echo("      endpoints:")
        click.echo("        - name: my-webhook")
        click.echo("          url: https://example.com/hook")
        return

    click.echo(f"Webhook Endpoints ({len(endpoints)}):")
    click.echo()
    for endpoint in endpoints:
        status = "enabled" if endpoint.get("enabled", True) else "disabled"
        click.echo(f"  {endpoint['name']} [{status}]")
        click.echo(f"    URL: {endpoint.get('url', 'not configured')}")
        events = endpoint.get("events", [])
        if events:
            click.echo(f"    Events: {', '.join(events)}")
        else:
            click.echo("    Events: all")
        if endpoint.get("can_block"):
            click.echo("    Can block: yes")


@webhooks.command("test")
@click.argument("webhook_name")
@click.option(
    "--event",
    "-e",
    default="notification",
    help="Event type to send (default: notification)",
)
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def webhooks_test(ctx: click.Context, webhook_name: str, event: str, json_format: bool) -> None:
    """Test a webhook endpoint by sending a test event.

    WEBHOOK_NAME is the name of the webhook endpoint to test.
    """
    client = get_daemon_client(ctx)
    if not check_daemon_running(client):
        sys.exit(1)

    result = call_mcp_api(
        client,
        "/api/webhooks/test",
        method="POST",
        json_data={
            "name": webhook_name,
            "event_type": event,
        },
    )

    if result is None:
        click.echo(f"Failed to test webhook: {webhook_name}", err=True)
        sys.exit(1)

    if json_format:
        click.echo(json.dumps(result, indent=2))
        return

    if result.get("success"):
        click.echo(f"Webhook '{webhook_name}' test successful!")
        click.echo(f"  Status: {result.get('status_code', 'unknown')}")
        response_time = result.get("response_time_ms")
        if response_time:
            click.echo(f"  Response time: {response_time:.0f}ms")
    else:
        click.echo(f"Webhook test failed: {result.get('error', 'Unknown error')}", err=True)
        if result.get("status_code"):
            click.echo(f"  Status: {result.get('status_code')}")
        sys.exit(1)
