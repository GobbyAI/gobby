"""Operator approval for a session's writes to unowned dirty paths, sent to the daemon.

The daemon accepts the approval only when gdaemon verified the bootstrap operator
API key at its front door. This client sends one request carrying that key and
nothing else: no managed bearer, no X-Gobby identity headers, no proxy.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import click
import httpx

from gobby.utils.daemon_url import DaemonUrlError, daemon_url
from gobby.utils.local_token import read_local_api_token

_LOGIN_HINT = "operator API key unavailable or unregistered; run: gobby auth login"


def _refusal_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text
    return str(body.get("detail", body)) if isinstance(body, dict) else response.text


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

    Operator only; the rule block-dirty-path-approval-cli refuses it to every agent
    session. The daemon accepts the approval only from the bootstrap operator API
    key, so run `gobby auth login` first. SESSION_REF is a session UUID or
    <project>#N. Each path stays approved only while its content matches what it is
    now. A new approval replaces the session's earlier one for the checkout.
    """
    token = read_local_api_token()
    if token is None:
        raise click.ClickException(_LOGIN_HINT)
    checkout = str(Path(checkout_root).resolve())
    try:
        response = httpx.post(
            f"{daemon_url()}/api/sessions/{quote(session_ref, safe='')}/dirty-path-approvals",
            json={"checkout_root": checkout, "paths": list(paths)},
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            timeout=30.0,
        )
    except (DaemonUrlError, httpx.HTTPError) as exc:
        raise click.ClickException(f"daemon request failed: {exc}") from exc
    if response.status_code == 401:
        raise click.ClickException(_LOGIN_HINT)
    if not response.is_success:
        raise click.ClickException(
            f"approval refused ({response.status_code}): {_refusal_detail(response)}"
        )
    body = response.json()
    click.echo(f"Approved for session {body['session_id']} in {checkout}:")
    for path, fingerprint in sorted(body["approvals"].items()):
        click.echo(f"  {path} ({fingerprint or 'absent'})")
