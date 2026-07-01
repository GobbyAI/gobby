"""CLI commands for managing encrypted secrets."""

import os

import click

from gobby.storage.hub.runtime import open_runtime_hub_database
from gobby.storage.secrets import (
    POSTURE_KEY_FILE,
    POSTURE_SCRYPT_PASSPHRASE,
    SECRET_KEK_PASSPHRASE_ENV,
    VALID_CATEGORIES,
    SecretMigrationError,
    SecretMigrationReport,
    SecretStore,
)


class _SecretStoreContext:
    """Context manager that ensures the DB is closed after use."""

    def __enter__(self) -> SecretStore:
        try:
            self._db = open_runtime_hub_database()
        except (RuntimeError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        return SecretStore(self._db)

    def __exit__(self, *args: object) -> None:
        self._db.close()


def _get_secret_store() -> SecretStore:
    """Open the active hub and return a SecretStore (no daemon required).

    NOTE: For proper cleanup, prefer using _SecretStoreContext() as a context manager.
    Kept for backward compatibility with existing callers.
    """
    try:
        db = open_runtime_hub_database()
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    return SecretStore(db)


def _display_posture(posture: str | None) -> str:
    return (posture or POSTURE_KEY_FILE).replace("_", "-")


def _prompt_kek_passphrase() -> str:
    passphrase = os.environ.get(SECRET_KEK_PASSPHRASE_ENV)
    if passphrase:
        return passphrase
    return str(
        click.prompt(
            "Secret KEK passphrase",
            hide_input=True,
            confirmation_prompt=True,
        )
    )


def _echo_migration_report(report: SecretMigrationReport) -> None:
    mode = "dry run" if report.dry_run else "migration"
    click.echo(
        f"Secret {mode}: total={report.total}, migrated={report.migrated}, "
        f"skipped={report.skipped}, failed={report.failed}"
    )
    for entry in report.entries:
        reason = f" ({entry.reason})" if entry.reason else ""
        required = " required" if entry.required else ""
        click.echo(f"  {entry.name}: {entry.status}{required}{reason}")


@click.group()
def secrets() -> None:
    """Manage encrypted secrets (API keys, tokens, etc.)."""


@secrets.command("set")
@click.argument("name")
@click.option(
    "--category",
    type=click.Choice(sorted(VALID_CATEGORIES), case_sensitive=False),
    default="general",
    help="Secret category.",
)
@click.option("--description", "-d", default=None, help="Human-readable description.")
@click.option(
    "--stdin",
    "from_stdin",
    is_flag=True,
    default=False,
    help="Read value from stdin (non-interactive, for scripting).",
)
def set_secret(name: str, category: str, description: str | None, from_stdin: bool) -> None:
    """Store a secret. Value is prompted interactively (never passed as an argument).

    NAME is the secret identifier (e.g. anthropic_api_key). Reference it
    elsewhere as $secret:NAME.
    """
    if from_stdin:
        import sys

        value = sys.stdin.read().strip()
    else:
        value = click.prompt("Secret value", hide_input=True)
    if not value.strip():
        click.echo("Error: Secret value cannot be empty.", err=True)
        raise SystemExit(1)

    click.echo(f"Received {len(value)} characters.")
    with _SecretStoreContext() as store:
        info = store.set(name, value, category=category, description=description)
    click.echo(f"Stored secret '{info.name}' (category={info.category}).")


@secrets.command("list")
def list_secrets() -> None:
    """List stored secrets (metadata only, never values)."""
    with _SecretStoreContext() as store:
        items = store.list()
    if not items:
        click.echo("No secrets stored.")
        return

    # Simple table output
    name_width = max(len(s.name) for s in items)
    cat_width = max(len(s.category) for s in items)
    click.echo(f"{'NAME':<{name_width}}  {'CATEGORY':<{cat_width}}  DESCRIPTION")
    click.echo(f"{'-' * name_width}  {'-' * cat_width}  {'-' * 11}")
    for s in items:
        desc = s.description or ""
        click.echo(f"{s.name:<{name_width}}  {s.category:<{cat_width}}  {desc}")


@secrets.command("delete")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def delete_secret(name: str, yes: bool) -> None:
    """Delete a secret by NAME."""
    with _SecretStoreContext() as store:
        if not store.exists(name):
            click.echo(f"Secret '{name}' not found.", err=True)
            raise SystemExit(1)

        if not yes:
            click.confirm(f"Delete secret '{name}'?", abort=True)

        store.delete(name)
    click.echo(f"Deleted secret '{name}'.")


@secrets.command("get")
@click.argument("name")
def get_secret(name: str) -> None:
    """Check if a secret exists (does NOT reveal the value)."""
    with _SecretStoreContext() as store:
        exists = store.exists(name)
    if exists:
        click.echo(f"Secret '{name}' exists.")
    else:
        click.echo(f"Secret '{name}' not found.", err=True)
        raise SystemExit(1)


@secrets.command("migrate")
@click.option("--dry-run", is_flag=True, help="Report legacy migration without writing changes.")
def migrate_secrets(dry_run: bool) -> None:
    """Migrate legacy machine-bound secrets to envelope encryption."""
    with _SecretStoreContext() as store:
        try:
            report = store.migrate_legacy_machine_id_secrets(dry_run=dry_run)
        except SecretMigrationError as exc:
            _echo_migration_report(exc.report)
            raise click.ClickException(str(exc)) from exc
    _echo_migration_report(report)


@secrets.command("rekey")
@click.option(
    "--posture",
    type=click.Choice(["key-file", "passphrase"]),
    default="key-file",
    show_default=True,
    help="KEK posture used to wrap the DEK.",
)
def rekey_secrets(posture: str) -> None:
    """Re-wrap the DEK without re-encrypting secret values."""
    storage_posture = POSTURE_SCRYPT_PASSPHRASE if posture == "passphrase" else POSTURE_KEY_FILE
    new_passphrase = (
        _prompt_kek_passphrase() if storage_posture == POSTURE_SCRYPT_PASSPHRASE else None
    )
    with _SecretStoreContext() as store:
        before = store.current_kek_posture()
        if before == POSTURE_SCRYPT_PASSPHRASE:
            current_passphrase = os.environ.get(SECRET_KEK_PASSPHRASE_ENV)
            if not current_passphrase:
                current_passphrase = str(
                    click.prompt(
                        "Current secret KEK passphrase",
                        hide_input=True,
                    )
                )
            store.kek_passphrase = current_passphrase
        store.set_kek_posture(storage_posture, passphrase=new_passphrase)
        after = store.current_kek_posture()
    click.echo(f"Re-wrapped secret DEK: {_display_posture(before)} -> {_display_posture(after)}")
