"""CLI command for managing web UI authentication."""

from contextlib import nullcontext

import click

from gobby.cli.auth_login import key, login
from gobby.cli.runtime import require_cli_database
from gobby.identity import hash_password, validate_password
from gobby.storage.users import LocalUserManager


@click.group("auth")
def auth() -> None:
    """Manage web credentials, node enrollment, and API keys."""


auth.add_command(login)
auth.add_command(key)


@auth.command("credentials")
def credentials() -> None:
    """Reset the installed user's web UI password."""
    try:
        with nullcontext(require_cli_database()) as db:
            users = LocalUserManager(db)
            user = users.require_sole_user()
            click.echo(f"Resetting web UI password for {user.email}.")
            password = validate_password(
                str(click.prompt("New password", hide_input=True, confirmation_prompt=True))
            )
            users.update_password(user.id, hash_password(password))
            click.echo(f"Password updated for {user.email}.")
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
