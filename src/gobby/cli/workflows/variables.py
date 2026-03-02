"""Variable management commands for workflows."""

import json

import click

from gobby.cli.workflows import common


@click.command("set-var")
@click.argument("name")
@click.argument("value")
@click.option("--session", "-s", "session_id", help="Session ID (defaults to current)")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def set_variable(
    ctx: click.Context, name: str, value: str, session_id: str | None, json_format: bool
) -> None:
    """Set a workflow variable for the current session.

    Variables are session-scoped (not persisted to YAML files).

    Examples:

        gobby workflows set-var session_epic #47

        gobby workflows set-var is_worktree true

        gobby workflows set-var max_retries 5
    """
    session_var_manager = common.get_session_var_manager()

    session_id = common.resolve_session_id(session_id)

    # Parse value type
    parsed_value: str | int | float | bool | None
    value_lower = value.lower()
    if value_lower in ("null", "none"):
        parsed_value = None
    elif value_lower == "true":
        parsed_value = True
    elif value_lower == "false":
        parsed_value = False
    else:
        # Try int, then float, then string
        try:
            parsed_value = int(value)
        except ValueError:
            try:
                parsed_value = float(value)
            except ValueError:
                parsed_value = value

    # Set the variable
    session_var_manager.set_variable(session_id, name, parsed_value)

    if json_format:
        all_variables = session_var_manager.get_variables(session_id)
        click.echo(
            json.dumps(
                {
                    "success": True,
                    "session_id": session_id,
                    "variable": name,
                    "value": parsed_value,
                    "all_variables": all_variables,
                },
                indent=2,
            )
        )
    else:
        value_display = repr(parsed_value) if isinstance(parsed_value, str) else str(parsed_value)
        click.echo(f"✓ Set {name} = {value_display}")
        click.echo(f"  Session: {common.truncate_id(session_id)}")


@click.command("get-var")
@click.argument("name", required=False)
@click.option("--session", "-s", "session_id", help="Session ID (defaults to current)")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def get_variable(
    ctx: click.Context, name: str | None, session_id: str | None, json_format: bool
) -> None:
    """Get workflow variable(s) for the current session.

    If NAME is provided, shows that specific variable.
    If NAME is omitted, shows all variables.

    Examples:

        gobby workflows get-var session_epic

        gobby workflows get-var
    """
    session_var_manager = common.get_session_var_manager()

    session_id = common.resolve_session_id(session_id)

    variables = session_var_manager.get_variables(session_id)

    if name:
        # Get specific variable
        exists = name in variables
        value = variables.get(name)

        if json_format:
            click.echo(
                json.dumps(
                    {
                        "success": True,
                        "session_id": session_id,
                        "variable": name,
                        "value": value,
                        "exists": exists,
                    },
                    indent=2,
                )
            )
        else:
            if exists:
                value_display = repr(value) if isinstance(value, str) else str(value)
                click.echo(f"{name} = {value_display}")
            else:
                click.echo(f"{name}: not set")
    else:
        # Get all variables
        if json_format:
            click.echo(
                json.dumps(
                    {
                        "success": True,
                        "session_id": session_id,
                        "variables": variables,
                    },
                    indent=2,
                )
            )
        else:
            if variables:
                click.echo(f"Variables for session {common.truncate_id(session_id)}:\n")
                for var_name, var_value in sorted(variables.items()):
                    value_display = (
                        repr(var_value) if isinstance(var_value, str) else str(var_value)
                    )
                    click.echo(f"  {var_name} = {value_display}")
            else:
                click.echo(f"No variables set for session {common.truncate_id(session_id)}")
