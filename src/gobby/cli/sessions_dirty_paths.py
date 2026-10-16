"""Operator approval for a session's writes to unowned dirty paths."""

from __future__ import annotations

import getpass

import click

from gobby.cli.runtime import require_cli_database
from gobby.cli.utils import resolve_session_id
from gobby.workflows.dirty_edit_grants import record_dirty_path_approval


@click.command("approve-dirty-paths")
@click.argument("session_ref")
@click.option(
    "--checkout",
    "checkout_root",
    required=True,
    type=click.Path(exists=True, file_okay=False),
    help="Checkout root the paths belong to.",
)
@click.argument("paths", nargs=-1, required=True)
def approve_dirty_paths(session_ref: str, checkout_root: str, paths: tuple[str, ...]) -> None:
    """Let SESSION_REF write exactly PATHS, dirty with no live owner, in one checkout.

    Operator only; the rule block-dirty-path-approval-cli refuses it to every
    agent session. Each path stays approved only while its content matches what
    it is now. A new approval replaces the session's earlier one for the checkout.
    """
    session_id = resolve_session_id(session_ref)
    try:
        fingerprints = record_dirty_path_approval(
            require_cli_database(),
            session_id,
            checkout_root=checkout_root,
            paths=paths,
            approved_by=getpass.getuser(),
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Approved for session {session_id} in {checkout_root}:")
    for path, fingerprint in sorted(fingerprints.items()):
        click.echo(f"  {path} ({fingerprint or 'absent'})")
