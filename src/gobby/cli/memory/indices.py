from __future__ import annotations

import importlib
import urllib.parse
from types import ModuleType

import click
import httpx


def _facade() -> ModuleType:
    return importlib.import_module("gobby.cli.memory")


@click.command("reindex-embeddings")
@click.pass_context
def reindex_embeddings(ctx: click.Context) -> None:
    """Regenerate embeddings for all memories.

    Generates embedding vectors for all stored memories using the configured
    embedding model. Useful after changing models or for initial setup.

    Requires the Gobby daemon to be running (delegates via HTTP API).

    Examples:

        gobby memory reindex-embeddings
    """
    memory_module = _facade()
    client = memory_module._get_daemon_client(ctx)
    click.echo("Reindexing memory embeddings...")
    try:
        response = client.call_http_api(
            "/api/memories/embeddings/reindex", method="POST", timeout=300.0
        )
        result = response.json()
    except (httpx.HTTPError, ConnectionError, OSError, ValueError) as e:
        click.echo(f"Error: Could not reach daemon — is it running? ({e})")
        raise SystemExit(1) from e

    if result.get("success", False):
        total = result.get("total_memories", 0)
        generated = result.get("embeddings_generated", 0)
        click.echo(f"Reindexed {generated}/{total} memory embeddings.")
    else:
        click.echo(f"Error: {result.get('error', 'Unknown error')}")


@click.command("reconcile")
@click.option("--dry-run", is_flag=True, help="Report orphans without deleting")
@click.pass_context
def reconcile(ctx: click.Context, dry_run: bool) -> None:
    """Reconcile Qdrant and Neo4j with the PostgreSQL hub source of truth.

    Finds orphaned vectors and graph nodes whose memory IDs no longer
    exist in the PostgreSQL hub, and optionally deletes them.

    Requires the Gobby daemon to be running (delegates via HTTP API).

    Examples:

        gobby memory reconcile --dry-run

        gobby memory reconcile
    """
    memory_module = _facade()
    client = memory_module._get_daemon_client(ctx)
    mode = "Dry-run: scanning" if dry_run else "Reconciling"
    click.echo(f"{mode} memory stores...")
    try:
        params = urllib.parse.urlencode({"dry_run": str(dry_run).lower()})
        response = client.call_http_api(
            f"/api/memories/reconcile?{params}", method="POST", timeout=600.0
        )
        result = response.json()
    except (httpx.HTTPError, ConnectionError, OSError, ValueError) as e:
        click.echo(f"Error: Could not reach daemon — is it running? ({e})")
        raise SystemExit(1) from e

    qdrant = result.get("qdrant", {})
    neo4j = result.get("neo4j", {})
    storage_count = result.get("storage_count", result.get("sqlite_count", "?"))
    click.echo(f"Hub memories: {storage_count}")
    click.echo(
        f"Qdrant: {qdrant.get('orphans_found', 0)} orphans found, "
        f"{qdrant.get('orphans_deleted', 0)} deleted"
    )
    click.echo(
        f"Neo4j: {neo4j.get('orphan_memories_found', 0)} orphan memories, "
        f"{neo4j.get('orphan_memories_deleted', 0)} deleted; "
        f"{neo4j.get('orphan_entities_deleted', 0)} orphan entities cleaned"
    )
    if dry_run:
        click.echo("(dry run — no changes made)")


@click.command("rebuild-crossrefs")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.pass_context
def rebuild_crossrefs(ctx: click.Context, project_ref: str | None) -> None:
    """Rebuild cross-references between memories (requires running daemon).

    Uses vector similarity to find related memories and create links.
    These links power the 2D memory graph visualization.

    Examples:

        gobby memory rebuild-crossrefs

        gobby memory rebuild-crossrefs -p myproject
    """
    memory_module = _facade()
    client = memory_module._get_daemon_client(ctx)
    is_healthy, err = client.check_health()
    if not is_healthy:
        raise click.ClickException(f"Daemon not running: {err}")

    click.echo("Rebuilding cross-references (this may take a while)...")
    project_id = (
        memory_module.resolve_project_ref(project_ref, exit_on_not_found=True)
        if project_ref
        else None
    )
    params = f"?project_id={urllib.parse.quote(str(project_id))}" if project_id else ""
    response = client.call_http_api(
        f"/api/memories/crossrefs/rebuild{params}", method="POST", timeout=600.0
    )
    if not response.is_success:
        raise click.ClickException(f"Rebuild failed (HTTP {response.status_code}): {response.text}")
    try:
        data = response.json()
    except ValueError as e:
        raise click.ClickException(f"Invalid response from daemon: {e}") from e
    click.echo(
        f"Done: {data.get('memories_processed', '?')} memories processed, "
        f"{data.get('crossrefs_created', '?')} crossrefs created"
    )
