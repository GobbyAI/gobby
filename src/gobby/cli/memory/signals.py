"""Recall-signal hub backfill and drift-check commands (#17196, #17201)."""

from __future__ import annotations

import json
from pathlib import Path

import click

from gobby.memory.recall_signal_log import (
    resolve_recall_signal_path,
    rotated_recall_signal_paths,
)
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

    Without --path, rotated backups ('.2', '.1') load oldest-first before the
    live file. Idempotent: rows keyed by (session_id, recall_request_id) that
    already exist are skipped.
    """
    if path_ is not None:
        if not path_.exists():
            raise click.ClickException(f"No recall-signal log at {path_}")
        sources = [path_]
    else:
        resolved = resolve_recall_signal_path(None)
        sources = rotated_recall_signal_paths(resolved)
        if not sources:
            raise click.ClickException(f"No recall-signal log at {resolved}")
    db = open_runtime_hub_database(apply_migrations=False)
    store = RecallSignalStore(db)
    for source in sources:
        inserted = store.load_signal_events_jsonl(source)
        click.echo(f"Inserted {inserted} recall-signal request rows from {source}")


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


@recall_signals.command("drift")
@click.option(
    "--threshold",
    type=float,
    default=None,
    help="Alarm accuracy-drop threshold (defaults to memory.recall_drift_accuracy_drop).",
)
@click.option(
    "--min-pairs",
    type=int,
    default=None,
    help="Pair floor for both the live provenance cohort and recorded holdout baseline.",
)
@click.pass_context
def drift(
    ctx: click.Context,
    threshold: float | None,
    min_pairs: int | None,
) -> None:
    """Check live recall quality against the recorded holdout baseline (#17201).

    Prints the drift report as JSON and exits 1 when the regression alarm
    fires. The alarm's response path is the #17200 one-flag rollback:
    ``memory.use_fitted_recall_constants=false``.
    """
    from gobby.config.app import DaemonConfig
    from gobby.memory.recall_drift import DriftThresholds, run_drift_check_from_store

    config = ctx.obj.get("config") if isinstance(ctx.obj, dict) else None
    if not isinstance(config, DaemonConfig):
        raise click.ClickException("Daemon config is unavailable in CLI context")
    memory_config = config.memory

    thresholds = None
    if threshold is not None or min_pairs is not None:
        defaults = DriftThresholds(accuracy_drop=memory_config.recall_drift_accuracy_drop)
        thresholds = DriftThresholds(
            accuracy_drop=threshold if threshold is not None else defaults.accuracy_drop,
            min_pairs=min_pairs if min_pairs is not None else defaults.min_pairs,
        )

    db = open_runtime_hub_database(apply_migrations=False)
    report = run_drift_check_from_store(
        RecallSignalStore(db),
        memory_config,
        thresholds=thresholds,
    )
    click.echo(json.dumps(report.to_record(), indent=2))
    if report.alarm:
        raise SystemExit(1)
