"""Exact, manifest-bound cleanup for recall/debug FalkorDB graphs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Protocol, cast

import click
import redis

from gobby.cli.runtime import get_cli_runtime

_RESERVED_GRAPHS = frozenset({"gobby_code", "gobby_wiki", "gobby_kg", "gwiki"})
_OWNED_GRAPH_PATTERNS = (
    re.compile(r"^test_recall_benchmark_.+$"),
    re.compile(r"^test_recall_benchmark_e2e_.+$"),
    re.compile(r"^dbg.*_.*$"),
    re.compile(r"^probe_cluster_.+$"),
)


class _RedisCommandClient(Protocol):
    def execute_command(self, *args: str) -> object: ...

    def close(self) -> None: ...


@click.group("recall-maintenance")
def recall_maintenance() -> None:
    """Perform narrowly scoped recall/debug graph maintenance."""


@recall_maintenance.command("drop-graph")
@click.argument("graph")
@click.option("--manifest", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--manifest-sha256", required=True)
def drop_graph(graph: str, manifest: Path, manifest_sha256: str) -> None:
    """Delete one exact graph named by a sealed reconciliation manifest."""
    _validate_target(graph, manifest, manifest_sha256)
    if graph not in _list_graphs():
        click.echo(f"FalkorDB graph already absent: {graph}")
        return
    _delete_graph(graph)
    click.echo(f"Deleted FalkorDB graph: {graph}")


def _validate_target(graph: str, manifest: Path, expected_sha256: str) -> None:
    if graph in _RESERVED_GRAPHS:
        raise click.ClickException(f"Refusing to delete reserved FalkorDB graph: {graph}")
    if not any(pattern.fullmatch(graph) for pattern in _OWNED_GRAPH_PATTERNS):
        raise click.ClickException(f"Graph is outside recall-maintenance owned patterns: {graph}")

    payload = manifest.read_bytes()
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256.lower():
        raise click.ClickException(
            f"Manifest sha256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Invalid deletion manifest JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise click.ClickException("Deletion manifest must be a JSON object")
    if document.get("manifest_format") != "gobby-vector-graph-reconcile-deletion":
        raise click.ClickException("Unexpected deletion manifest format")
    if document.get("manifest_version") != 1:
        raise click.ClickException("Unexpected deletion manifest version")
    deletions = document.get("deletions")
    if not isinstance(deletions, list):
        raise click.ClickException("Deletion manifest has no deletions list")
    matches = [
        item
        for item in deletions
        if isinstance(item, dict)
        and item.get("store") == "falkordb"
        and item.get("namespace") == graph
        and item.get("tier") == 3
        and item.get("disposition") == "delete"
        and item.get("owner") == "recall-maintenance"
    ]
    if len(matches) != 1:
        raise click.ClickException(f"Manifest does not authorize exact FalkorDB graph: {graph}")
    original_inventory = document.get("original_inventory")
    if (
        not isinstance(original_inventory, dict)
        or not isinstance(original_inventory.get("falkordb"), list)
        or graph not in original_inventory["falkordb"]
    ):
        raise click.ClickException(
            "Graph is absent from the manifest's original FalkorDB inventory"
        )


def _falkor_connection() -> tuple[str, int, str | None]:
    config = get_cli_runtime().require_config(apply_migrations=False).databases.falkordb
    return config.host, config.port, config.password


def _list_graphs() -> list[str]:
    host, port, password = _falkor_connection()
    client = cast(
        _RedisCommandClient,
        redis.Redis(host=host, port=port, password=password, decode_responses=True),
    )
    try:
        result = client.execute_command("GRAPH.LIST")
        return sorted(cast(list[str], result))
    except redis.RedisError as exc:
        raise click.ClickException(f"FalkorDB GRAPH.LIST failed: {exc}") from exc
    finally:
        client.close()


def _delete_graph(graph: str) -> None:
    host, port, password = _falkor_connection()
    client = cast(
        _RedisCommandClient,
        redis.Redis(host=host, port=port, password=password, decode_responses=True),
    )
    try:
        client.execute_command("GRAPH.DELETE", graph)
    except redis.RedisError as exc:
        raise click.ClickException(f"FalkorDB GRAPH.DELETE failed for {graph}: {exc}") from exc
    finally:
        client.close()
