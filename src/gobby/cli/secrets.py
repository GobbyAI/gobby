"""CLI commands for managing encrypted secrets."""

import os
from pathlib import Path

import click

from gobby.cli.runtime import require_cli_database
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import (
    POSTURE_KEY_FILE,
    POSTURE_SCRYPT_PASSPHRASE,
    SECRET_KEK_PASSPHRASE_ENV,
    VALID_CATEGORIES,
    SecretStore,
)

NEW_SECRET_KEK_PASSPHRASE_ENV = "GOBBY_NEW_SECRET_KEK_PASSPHRASE"


class _SecretStoreContext:
    """Context manager that borrows the CLI runtime database."""

    def __enter__(self) -> SecretStore:
        try:
            db = require_cli_database()
        except (RuntimeError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        return SecretStore(db)

    def __exit__(self, *args: object) -> None:
        return None


def _display_posture(posture: str | None) -> str:
    return (posture or POSTURE_KEY_FILE).replace("_", "-")


def _resolve_cli_secret_scope(
    db: HubDatabase,
    *,
    global_scope: bool,
    project_ref: str | None,
) -> tuple[str | None, str]:
    if global_scope and project_ref:
        click.echo("Choose either --project or --global.", err=True)
        raise SystemExit(1)
    if global_scope:
        return None, "global"
    if project_ref:
        from gobby.storage.projects import LocalProjectManager

        project = LocalProjectManager(db).resolve_ref(project_ref)
        if project is None:
            click.echo(f"Project not found: {project_ref}", err=True)
            raise SystemExit(1)
        return project.id, f"project {project.name}"
    from gobby.cli.installers.shared import registered_project_id
    from gobby.storage.projects import LocalProjectManager

    project_id = registered_project_id(db, Path.cwd())
    if project_id is None:
        return None, "global"
    project = LocalProjectManager(db).get(project_id)
    label = project.name if project is not None else project_id
    return project_id, f"project {label}"


def _prompt_kek_passphrase() -> str:
    passphrase = os.environ.get(NEW_SECRET_KEK_PASSPHRASE_ENV)
    if passphrase:
        return passphrase
    return str(
        click.prompt(
            "Secret KEK passphrase",
            hide_input=True,
            confirmation_prompt=True,
        )
    )


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
@click.option(
    "--global", "global_scope", is_flag=True, default=False, help="Store in global scope."
)
@click.option("--project", "project_ref", default=None, help="Project UUID or name.")
def set_secret(
    name: str,
    category: str,
    description: str | None,
    from_stdin: bool,
    global_scope: bool,
    project_ref: str | None,
) -> None:
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
        from gobby.storage.config_store import ConfigStore

        project_id, scope_label = _resolve_cli_secret_scope(
            store.db,
            global_scope=global_scope,
            project_ref=project_ref,
        )
        info = ConfigStore(store.db).set_named_secret(
            store,
            name,
            value,
            category=category,
            description=description,
            project_id=project_id,
        )
    click.echo(f"Stored secret '{info.name}' (scope: {scope_label}).")


@secrets.command("list")
@click.option("--global", "global_scope", is_flag=True, default=False, help="List global secrets.")
@click.option("--project", "project_ref", default=None, help="Project UUID or name.")
def list_secrets(global_scope: bool, project_ref: str | None) -> None:
    """List stored secrets (metadata only, never values)."""
    with _SecretStoreContext() as store:
        project_id, _scope_label = _resolve_cli_secret_scope(
            store.db,
            global_scope=global_scope,
            project_ref=project_ref,
        )
        items = store.list(project_id=project_id)
    if not items:
        click.echo("No secrets stored.")
        return

    name_width = max(len(s.name) for s in items)
    cat_width = max(len(s.category) for s in items)
    scope_width = max(5, max(len(s.scope) for s in items))
    click.echo(
        f"{'NAME':<{name_width}}  {'CATEGORY':<{cat_width}}  {'SCOPE':<{scope_width}}  DESCRIPTION"
    )
    click.echo(f"{'-' * name_width}  {'-' * cat_width}  {'-' * scope_width}  {'-' * 11}")
    for s in items:
        desc = s.description or ""
        click.echo(
            f"{s.name:<{name_width}}  {s.category:<{cat_width}}  {s.scope:<{scope_width}}  {desc}"
        )


@secrets.command("delete")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.option("--global", "global_scope", is_flag=True, default=False, help="Delete global secret.")
@click.option("--project", "project_ref", default=None, help="Project UUID or name.")
def delete_secret(
    name: str,
    yes: bool,
    global_scope: bool,
    project_ref: str | None,
) -> None:
    """Delete a secret by NAME."""
    with _SecretStoreContext() as store:
        project_id, scope_label = _resolve_cli_secret_scope(
            store.db,
            global_scope=global_scope,
            project_ref=project_ref,
        )
        if not store.exists(name, project_id=project_id):
            click.echo(f"Secret '{name}' not found.", err=True)
            raise SystemExit(1)

        if not yes:
            click.confirm(f"Delete secret '{name}'?", abort=True)

        from gobby.storage.config_store import ConfigStore

        deleted = ConfigStore(store.db).delete_named_secret(store, name, project_id=project_id)
        if not deleted:
            from gobby.storage.secret_names import normalize_secret_name

            referenced = normalize_secret_name(name) in store.find_persisted_secret_references(
                project_id=project_id
            )
            if referenced:
                click.echo(
                    f"Secret '{name}' is still referenced by stored configuration; not deleted.",
                    err=True,
                )
            else:
                click.echo(f"Secret '{name}' not found in {scope_label} scope.", err=True)
            raise SystemExit(1)
    click.echo(f"Deleted secret '{name}'.")


@secrets.command("get")
@click.argument("name")
@click.option(
    "--global", "global_scope", is_flag=True, default=False, help="Look up global secret."
)
@click.option("--project", "project_ref", default=None, help="Project UUID or name.")
def get_secret(name: str, global_scope: bool, project_ref: str | None) -> None:
    """Check if a secret exists (does NOT reveal the value)."""
    with _SecretStoreContext() as store:
        project_id, _scope_label = _resolve_cli_secret_scope(
            store.db,
            global_scope=global_scope,
            project_ref=project_ref,
        )
        exists = store.exists(name, project_id=project_id)
    if exists:
        click.echo(f"Secret '{name}' exists.")
    else:
        click.echo(f"Secret '{name}' not found.", err=True)
        raise SystemExit(1)


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
