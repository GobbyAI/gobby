"""CLI debug surface for unmodeled transcript observations."""

from __future__ import annotations

import json

import click

from gobby.storage.hub.runtime import open_runtime_hub_database
from gobby.storage.unmodeled_observations import (
    COUNT_SEMANTICS,
    UnmodeledObservationStore,
)


@click.group("observations")
def observations() -> None:
    """Inspect unmodeled transcript observations."""


@observations.command("list")
@click.option("--source", help="Filter by transcript source")
@click.option("--kind", help="Filter by observation kind")
@click.option("--limit", default=50, show_default=True, type=click.IntRange(1, 500))
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def list_observations(
    source: str | None,
    kind: str | None,
    limit: int,
    json_format: bool,
) -> None:
    """List unmodeled observations sorted by count."""
    db = open_runtime_hub_database(apply_migrations=False)
    rows = UnmodeledObservationStore(db).list_observations(
        source=source,
        kind=kind,
        limit=limit,
    )
    payload = {
        "count_semantics": COUNT_SEMANTICS,
        "observations": [row.__dict__ for row in rows],
    }
    if json_format:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    click.echo(f"Count semantics: {COUNT_SEMANTICS}")
    if not rows:
        click.echo("No unmodeled observations found.")
        return

    for row in rows:
        tool_bits = []
        if row.server_name:
            tool_bits.append(f"server={row.server_name}")
        if row.tool_type:
            tool_bits.append(f"type={row.tool_type}")
        suffix = f" ({', '.join(tool_bits)})" if tool_bits else ""
        click.echo(f"{row.count:>6} {row.source} {row.kind}:{row.name}{suffix}")
        click.echo(f"       last_seen={row.last_seen_at} example_session={row.example_session_id}")
        click.echo(f"       sample_hash={row.sample_hash} sample_keys={', '.join(row.sample_keys)}")
