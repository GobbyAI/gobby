from __future__ import annotations

import importlib
import json
import time
import urllib.parse
from types import ModuleType

import click
import httpx


def _facade() -> ModuleType:
    return importlib.import_module("gobby.cli.memory")


@click.command("clear-graph")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def clear_graph(ctx: click.Context, project_ref: str | None, yes: bool) -> None:
    """Clear only the FalkorDB knowledge graph projection (requires running daemon).

    Deletes the KG projection in FalkorDB and marks affected memories pending
    so they can be extracted again. Does not clear vectors, crossrefs, or FTS.

    Examples:

        gobby memory clear-graph

        gobby memory clear-graph -p myproject

        gobby memory clear-graph --yes
    """
    memory_module = _facade()
    client = memory_module._get_daemon_client(ctx)
    is_healthy, err = client.check_health()
    if not is_healthy:
        raise click.ClickException(f"Daemon not running: {err}")

    project_id = memory_module.resolve_project_ref(
        project_ref, exit_on_not_found=project_ref is not None
    )
    if not project_id:
        if project_ref is not None:
            raise click.ClickException(
                f"Project '{project_ref}' was not found. "
                "Pass a valid -p value or check the identifier."
            )
        if not yes:
            click.confirm(
                "No project detected. This will clear the knowledge graph for ALL projects "
                "and requeue affected memories. Continue?",
                abort=True,
            )
    elif not yes:
        click.confirm(
            "This will clear the knowledge graph for this project and requeue affected "
            "memories. Continue?",
            abort=True,
        )

    params = f"?{urllib.parse.urlencode({'project_id': project_id})}" if project_id else ""
    click.echo("Clearing knowledge graph...")
    try:
        response = client.call_http_api(
            f"/api/memories/graph/clear{params}", method="POST", timeout=30.0
        )
    except (httpx.HTTPError, ConnectionError, OSError, ValueError) as e:
        click.echo(f"Error: Could not reach daemon — is it running? ({e})")
        raise SystemExit(1) from e

    if not response.is_success:
        raise click.ClickException(f"Clear failed (HTTP {response.status_code}): {response.text}")
    try:
        data = response.json()
    except ValueError as e:
        raise click.ClickException(f"Invalid response from daemon: {e}") from e

    scope = f"project {project_id}" if project_id else "all projects"
    click.echo(f"Knowledge graph cleared for {scope}.")
    click.echo(
        f"  Graph: {data.get('memories_deleted', 0)} memory nodes, "
        f"{data.get('entities_deleted', 0)} orphaned entities"
    )
    click.echo(f"  Requeued: {data.get('memories_marked_pending', 0)} memories")
    click.echo(
        "Use `gobby memory rebuild-graph` to repopulate immediately, or let the "
        "background worker refill it over time."
    )


@click.command("graph-counts")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.option("--json", "json_output", is_flag=True, help="Print raw JSON response")
@click.pass_context
def graph_counts(ctx: click.Context, project_ref: str | None, json_output: bool) -> None:
    """Show actual FalkorDB knowledge graph counts."""
    memory_module = _facade()
    client = memory_module._get_daemon_client(ctx)
    is_healthy, err = client.check_health()
    if not is_healthy:
        raise click.ClickException(f"Daemon not running: {err}")

    project_id = (
        memory_module.resolve_project_ref(project_ref, exit_on_not_found=True)
        if project_ref
        else None
    )
    params = f"?{urllib.parse.urlencode({'project_id': project_id})}" if project_id else ""
    try:
        response = client.call_http_api(
            f"/api/memories/graph/counts{params}", method="GET", timeout=30.0
        )
    except (httpx.HTTPError, ConnectionError, OSError, ValueError) as e:
        click.echo(f"Error: Could not reach daemon — is it running? ({e})")
        raise SystemExit(1) from e

    if not response.is_success:
        raise click.ClickException(f"Count failed (HTTP {response.status_code}): {response.text}")
    try:
        data = response.json()
    except ValueError as e:
        raise click.ClickException(f"Invalid response from daemon: {e}") from e

    if json_output:
        click.echo(json.dumps(data, sort_keys=True))
        return

    scope = f"project {project_id}" if project_id else "all projects"
    click.echo(f"Knowledge graph counts for {scope}:")
    click.echo(f"  Graph: {data.get('graph', 'gobby_kg')}")
    click.echo(f"  Nodes: {data.get('total_nodes', 0)} total")
    click.echo(f"    Memory: {data.get('memory_nodes', 0)}")
    click.echo(f"    Entity: {data.get('entity_nodes', 0)}")
    click.echo(f"    CodeSymbol: {data.get('code_symbol_nodes', 0)}")
    click.echo(f"  Relationships: {data.get('relationships', 0)} total")
    click.echo(f"    Entity: {data.get('entity_relationships', 0)}")
    click.echo(f"    MENTIONED_IN: {data.get('mentioned_in_relationships', 0)}")
    click.echo(f"    RELATES_TO_CODE: {data.get('relates_to_code_relationships', 0)}")


@click.command("rebuild-graph")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Wait for completion by polling daemon status",
)
@click.option(
    "--timeout",
    type=int,
    default=600,
    show_default=True,
    help="Maximum seconds to wait while polling rebuild status",
)
@click.pass_context
def rebuild_graph(ctx: click.Context, project_ref: str | None, wait: bool, timeout: int) -> None:
    """Extract entities from memories into the knowledge graph (requires running daemon).

    Processes all memories through LLM entity extraction and stores
    results in FalkorDB. Powers the 3D knowledge graph visualization.

    Examples:

        gobby memory rebuild-graph

        gobby memory rebuild-graph -p myproject

        gobby memory rebuild-graph --no-wait
    """
    memory_module = _facade()
    client = memory_module._get_daemon_client(ctx)
    is_healthy, err = client.check_health()
    if not is_healthy:
        raise click.ClickException(f"Daemon not running: {err}")

    project_id = (
        memory_module.resolve_project_ref(project_ref, exit_on_not_found=True)
        if project_ref
        else None
    )
    query_params: dict[str, str] = {"background": "true"}
    if project_id:
        query_params["project_id"] = str(project_id)
    params = f"?{urllib.parse.urlencode(query_params)}"
    response = client.call_http_api(
        f"/api/memories/graph/rebuild{params}", method="POST", timeout=30.0
    )
    if not response.is_success:
        raise click.ClickException(f"Rebuild failed (HTTP {response.status_code}): {response.text}")
    try:
        data = response.json()
    except ValueError as e:
        raise click.ClickException(f"Invalid response from daemon: {e}") from e

    job_id = data.get("job_id")
    if not job_id:
        raise click.ClickException("Invalid rebuild response: missing job_id")

    if data.get("already_running"):
        click.echo(f"Knowledge graph rebuild already running (job {job_id}).")
    else:
        click.echo(f"Started knowledge graph rebuild (job {job_id}).")

    if not wait:
        attach_cmd = "gobby memory rebuild-graph --wait"
        if project_ref:
            attach_cmd += f" -p {project_ref}"
        click.echo(f"Use `{attach_cmd}` to attach and poll progress, or check daemon logs.")
        return

    click.echo("Polling knowledge graph rebuild progress...")
    last_snapshot: tuple[object, ...] | None = None
    status_params = urllib.parse.urlencode({"job_id": str(job_id)})
    start_time = time.time()

    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout:
            raise click.ClickException(f"Rebuild job {job_id} timed out after {timeout} seconds")
        status_response = client.call_http_api(
            f"/api/memories/graph/rebuild/status?{status_params}",
            method="GET",
            timeout=30.0,
        )
        if not status_response.is_success:
            raise click.ClickException(
                f"Status check failed (HTTP {status_response.status_code}): {status_response.text}"
            )
        try:
            status_data = status_response.json()
        except ValueError as e:
            raise click.ClickException(f"Invalid status response from daemon: {e}") from e

        status = status_data.get("status", "unknown")
        total = int(status_data.get("memories_total") or 0)
        completed = int(status_data.get("memories_completed") or 0)
        errors = int(status_data.get("errors") or 0)
        counts = status_data.get("status_counts") or {}
        snapshot = (
            status,
            completed,
            total,
            errors,
            counts.get("success", 0),
            counts.get("noop_no_entities", 0),
        )
        if snapshot != last_snapshot:
            if status == "running":
                click.echo(
                    f"  progress: {completed}/{total or '?'} "
                    f"(success={counts.get('success', 0)}, "
                    f"noop={counts.get('noop_no_entities', 0)}, errors={errors})"
                )
            last_snapshot = snapshot

        if status == "completed":
            failed_memories = status_data.get("failed_memories") or []
            click.echo(
                f"Done: {status_data.get('result', {}).get('memories_extracted', '?')}/"
                f"{status_data.get('result', {}).get('memories_processed', total or '?')} "
                f"memories extracted, {errors} errors"
            )
            for failure in failed_memories:
                memory_id = failure.get("memory_id", "?")
                failure_status = failure.get("status", "unknown")
                failure_errors = failure.get("errors") or []
                detail = failure_errors[0] if failure_errors else "unknown error"
                click.echo(f"  failure: {memory_id} ({failure_status}) {detail}")
            return

        if status == "failed":
            detail = status_data.get("error") or "unknown error"
            raise click.ClickException(f"Rebuild job {job_id} failed: {detail}")

        time.sleep(3)


@click.command("invalidate")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def invalidate(ctx: click.Context, project_ref: str | None, yes: bool) -> None:
    """Wipe and rebuild ALL memory indices (embeddings, crossrefs, graph).

    Clears Qdrant vectors, FalkorDB graph, and crossrefs for the project,
    then starts a background rebuild from memory storage. The command
    returns as soon as indices are cleared.

    Examples:

        gobby memory invalidate

        gobby memory invalidate -p myproject

        gobby memory invalidate --yes
    """
    memory_module = _facade()
    client = memory_module._get_daemon_client(ctx)
    is_healthy, err = client.check_health()
    if not is_healthy:
        raise click.ClickException(f"Daemon not running: {err}")

    project_id = memory_module.resolve_project_ref(
        project_ref, exit_on_not_found=project_ref is not None
    )

    if not project_id:
        if project_ref is not None:
            raise click.ClickException(
                f"Project '{project_ref}' was not found. "
                "Pass a valid -p value or check the identifier."
            )
        if not yes:
            click.confirm(
                "No project detected. This will invalidate indices for ALL projects. Continue?",
                abort=True,
            )
    elif not yes:
        click.confirm(
            "This will wipe and rebuild all memory indices for this project. Continue?",
            abort=True,
        )

    params = f"?{urllib.parse.urlencode({'project_id': project_id})}" if project_id else ""
    click.echo("Clearing memory indices...")
    try:
        response = client.call_http_api(
            f"/api/memories/invalidate{params}", method="POST", timeout=30.0
        )
    except (httpx.HTTPError, ConnectionError, OSError, ValueError) as e:
        click.echo(f"Error: Could not reach daemon — is it running? ({e})")
        raise SystemExit(1) from e

    if not response.is_success:
        raise click.ClickException(
            f"Invalidate failed (HTTP {response.status_code}): {response.text}"
        )
    try:
        data = response.json()
    except ValueError as e:
        raise click.ClickException(f"Invalid response from daemon: {e}") from e

    graph_cleared = data.get("graph_cleared", {})
    if graph_cleared and not graph_cleared.get("skipped"):
        click.echo(
            f"  Graph: {graph_cleared.get('memories_deleted', 0)} memory nodes, "
            f"{graph_cleared.get('entities_deleted', 0)} orphaned entities"
        )
    if data.get("vectors_cleared"):
        click.echo("  Vectors: cleared")
    crossrefs = data.get("crossrefs_cleared")
    if crossrefs is not None:
        click.echo(f"  Crossrefs: {crossrefs} deleted")
    click.echo("Indices cleared. Rebuild started in background.")
    click.echo("Check daemon logs for rebuild progress.")
