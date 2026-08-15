"""CLI commands for session variables."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import click

from gobby.cli.runtime import require_cli_database
from gobby.cli.utils import resolve_session_id
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.json_helpers import json_dumps
from gobby.workflows.state_manager import SessionVariableManager

_db_instance: HubDatabase | None = None
_session_var_manager_instance: SessionVariableManager | None = None


def get_session_var_manager(db: HubDatabase | None = None) -> SessionVariableManager:
    """Return a session variable manager, caching the CLI-owned instance."""
    global _db_instance, _session_var_manager_instance
    if db is not None:
        return SessionVariableManager(db)
    if _session_var_manager_instance is None:
        _db_instance = require_cli_database()
        _session_var_manager_instance = SessionVariableManager(_db_instance)
    return _session_var_manager_instance


def close_session_var_manager() -> None:
    """Clear cached session variable manager references."""
    global _db_instance, _session_var_manager_instance
    _db_instance = None
    _session_var_manager_instance = None


@contextmanager
def session_var_manager_context(db: HubDatabase | None = None) -> Iterator[SessionVariableManager]:
    """Yield a session variable manager and clear cached references afterwards."""
    manager = get_session_var_manager(db)
    try:
        yield manager
    finally:
        if db is None:
            close_session_var_manager()


def _parse_variable_value(value: str) -> str | int | float | bool | None:
    value_lower = value.lower()
    if value_lower in ("null", "none"):
        return None
    if value_lower == "true":
        return True
    if value_lower == "false":
        return False
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


@click.group()
def variables() -> None:
    """Get and set session variables (defaults layered under session overrides)."""


@variables.command("set")
@click.argument("name")
@click.argument("value")
@click.option("--session", "-s", "session_id", help="Session ID (defaults to current)")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def set_variable(name: str, value: str, session_id: str | None, json_format: bool) -> None:
    """Set a session-scoped variable override.

    Reads still layer project/global defaults under session values.
    """
    with session_var_manager_context() as manager:
        resolved = resolve_session_id(session_id)
        parsed = _parse_variable_value(value)
        manager.set_variable(resolved, name, parsed)
        if json_format:
            click.echo(
                json_dumps(
                    {
                        "success": True,
                        "session_id": resolved,
                        "variable": name,
                        "value": parsed,
                        "all_variables": manager.get_variables(resolved),
                    },
                    indent=2,
                )
            )
            return
        value_display = repr(parsed) if isinstance(parsed, str) else str(parsed)
        click.echo(f"Set {name} = {value_display}")
        click.echo(f"  Session: {resolved}")


@variables.command("get")
@click.argument("name", required=False)
@click.option("--session", "-s", "session_id", help="Session ID (defaults to current)")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def get_variable(name: str | None, session_id: str | None, json_format: bool) -> None:
    """Get session variables, including layered definition defaults."""
    with session_var_manager_context() as manager:
        resolved = resolve_session_id(session_id)
        values = manager.get_variables(resolved)
        if name:
            exists = name in values
            value = values.get(name)
            if json_format:
                click.echo(
                    json_dumps(
                        {
                            "success": True,
                            "session_id": resolved,
                            "variable": name,
                            "value": value,
                            "exists": exists,
                        },
                        indent=2,
                    )
                )
                return
            if exists:
                value_display = repr(value) if isinstance(value, str) else str(value)
                click.echo(f"{name} = {value_display}")
            else:
                click.echo(f"{name}: not set")
            return
        if json_format:
            click.echo(
                json_dumps(
                    {"success": True, "session_id": resolved, "variables": values},
                    indent=2,
                )
            )
            return
        if values:
            click.echo(f"Variables for session {resolved}:\n")
            for var_name, var_value in sorted(values.items()):
                value_display = repr(var_value) if isinstance(var_value, str) else str(var_value)
                click.echo(f"  {var_name} = {value_display}")
        else:
            click.echo(f"No variables set for session {resolved}")
