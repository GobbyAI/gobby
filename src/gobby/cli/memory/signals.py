"""Recall-signal hub backfill commands (#17196, contract §5–§6)."""

from __future__ import annotations

from pathlib import Path

import click

from gobby.memory.recall_signal_log import resolve_recall_signal_path
from gobby.storage.hub.runtime import open_runtime_hub_database
from gobby.storage.recall_signals import RecallSignalStore


@click.group("recall-signals")
def recall_signals() -> None:
    """Manage promoted recall-signal hub tables."""


@recall_signals.command("backfill-events")
@click.option(
    "--path",
    "path_",
    type=click.Path(path_type=Path),
    default=None,
    help="recall_signal.jsonl path (defaults to ~/.gobby/logs/recall_signal.jsonl).",
)
def backfill_events(path_: Path | None) -> None:
    """Load recall_signal.jsonl events into the hub signal tables.

    Idempotent: rows keyed by (session_id, recall_request_id) that already
    exist are skipped.
    """
    resolved = path_ or resolve_recall_signal_path(None)
    if not resolved.exists():
        raise click.ClickException(f"No recall-signal log at {resolved}")
    db = open_runtime_hub_database(apply_migrations=False)
    inserted = RecallSignalStore(db).load_signal_events_jsonl(resolved)
    click.echo(f"Inserted {inserted} recall-signal request rows from {resolved}")


@recall_signals.command("backfill-labels")
@click.argument("path_", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def backfill_labels(path_: Path) -> None:
    """Load retrospective usefulness labels from a JSONL file.

    Accepts the #17193 calibration-dataset row shape (synthetic ``retro:``
    request ids). Idempotent on the contract §6 unique key.
    """
    db = open_runtime_hub_database(apply_migrations=False)
    inserted = RecallSignalStore(db).backfill_usefulness_labels_jsonl(path_)
    click.echo(f"Inserted {inserted} usefulness-label rows from {path_}")
