"""CLI command for managing web UI authentication."""

import click

from gobby.storage.auth import LOCAL_API_TOKEN_HASH_KEY, hash_token, rotate_local_api_token
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.runtime import runtime_hub_database
from gobby.storage.secrets import SecretStore
from gobby.utils.local_token import local_token_path, read_local_api_token


@click.group("auth")
def auth() -> None:
    """Manage web credentials and the local daemon API token."""


@auth.command("credentials")
@click.option("--remove", is_flag=True, help="Remove auth credentials and disable web UI login.")
def credentials(remove: bool) -> None:
    """Set up or reset web UI authentication credentials."""
    try:
        with runtime_hub_database(apply_migrations=False) as db:
            config_store = ConfigStore(db)
            secret_store = SecretStore(db)

            existing_username = config_store.get("auth.username")

            if remove:
                if not existing_username:
                    click.echo("No auth configured. Nothing to remove.")
                    return
                config_store.delete("auth.username")
                config_store.clear_secret("auth.password", secret_store)
                click.echo(f"Auth removed for user '{existing_username}'.")
                click.echo("Restart the daemon for changes to take effect.")
                return

            if existing_username:
                click.echo(f"Auth configured for user '{existing_username}'. Resetting password.")
                password = click.prompt("New password", hide_input=True, confirmation_prompt=True)
                config_store.set_secret("auth.password", password, secret_store, source="user")
                click.echo(f"Password updated for user '{existing_username}'.")
            else:
                click.echo("No auth configured. Setting up web UI authentication.")
                username = click.prompt("Username")
                password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
                config_store.set("auth.username", username, source="user")
                config_store.set_secret("auth.password", password, secret_store, source="user")
                click.echo(f"Auth enabled for user '{username}'.")
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo("Restart the daemon for changes to take effect.")


@auth.command("token")
@click.option("--show", is_flag=True, help="Show the plaintext local API token.")
@click.option("--rotate", is_flag=True, help="Rotate the local API token.")
def token(show: bool, rotate: bool) -> None:
    """Show token status. Repair mismatches with `gobby auth token --rotate`."""
    path = local_token_path()
    try:
        with runtime_hub_database(apply_migrations=False) as db:
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
