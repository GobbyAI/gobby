"""CLI command for managing web UI authentication."""

from contextlib import nullcontext

import click

from gobby.cli.runtime import require_cli_database
from gobby.identity import hash_password, validate_password
from gobby.storage.auth import (
    LOCAL_API_TOKEN_HASH_KEY,
    hash_token,
    rotate_local_api_token,
)
from gobby.storage.config_store import ConfigStore
from gobby.storage.users import LocalUserManager
from gobby.utils.local_token import local_token_path, read_local_api_token


@click.group("auth")
def auth() -> None:
    """Manage web credentials and the local daemon API token."""


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


@auth.command("token")
@click.option("--show", is_flag=True, help="Show the plaintext local API token.")
@click.option("--rotate", is_flag=True, help="Rotate the local API token.")
def token(show: bool, rotate: bool) -> None:
    """Show token status. Repair mismatches with `gobby auth token --rotate`."""
    path = local_token_path()
    try:
        with nullcontext(require_cli_database()) as db:
            config_store = ConfigStore(db)
            plaintext_token = read_local_api_token()
            if rotate:
                plaintext_token = rotate_local_api_token(config_store)
                click.echo("Local API token rotated.")
                click.echo("Clients on this machine will pick it up within ~5 seconds.")
                click.echo(f"Recopy {path} to any remote client machines.")

            stored_value = config_store.get(LOCAL_API_TOKEN_HASH_KEY)
            stored_hash = stored_value if isinstance(stored_value, str) else None
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    file_exists = plaintext_token is not None
    hashes_agree = (
        stored_hash is None
        if plaintext_token is None
        else stored_hash == hash_token(plaintext_token)
    )
    stored_hash_display = f"sha256:{stored_hash[:8]}…" if stored_hash else "missing"

    click.echo(f"Token path: {path}")
    click.echo(f"File: {'exists' if file_exists else 'missing'}")
    click.echo(f"Stored hash: {stored_hash_display}")
    click.echo(f"File and DB agree: {'yes' if hashes_agree else 'no'}")
    if show:
        if plaintext_token is None:
            raise click.ClickException(f"Local API token file is missing: {path}")
        click.echo(f"Token: {plaintext_token}")
