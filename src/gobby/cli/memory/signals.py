"""Recall-signal hub backfill and drift-check commands (#17196, #17201)."""

from __future__ import annotations

import getpass
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import click

from gobby.cli.runtime import get_cli_runtime, require_cli_database
from gobby.memory.recall_constants import RECALL_QUERY_CONSTRUCTION_VERSION
from gobby.memory.recall_fit import (
    CandidateFilterParams,
    WeightingMode,
    replay_candidate_filter,
    split_request_ids_per_project,
)
from gobby.memory.recall_ship_gate import (
    AUDIT_SAMPLE_REQUESTS,
    GateCohort,
    ShipAuditSample,
    build_ship_audit_sample,
    canonical_digest,
    evaluate_ship_audit,
)
from gobby.memory.recall_ship_gate_run import run_ship_gate_from_store
from gobby.memory.recall_signal_log import (
    resolve_recall_signal_path,
    rotated_recall_signal_paths,
)
from gobby.memory.services._search_constants import _GRAPH_CONFIDENCE_SELECTION_FLOOR
from gobby.memory.shadow_relevance import SHADOW_PROTOCOL_VERSION
from gobby.storage.recall_shadow_signals import ShadowCohortAmbiguityError
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
    db = require_cli_database()
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
    db = require_cli_database()
    inserted = RecallSignalStore(db).backfill_usefulness_labels_jsonl(path_)
    click.echo(f"Inserted {inserted} usefulness-label rows from {path_}")


class _AwareDateTime(click.ParamType[datetime]):
    name = "timezone-aware ISO-8601 timestamp"

    def convert(
        self,
        value: Any,
        param: click.Parameter | None,
        ctx: click.Context | None,
    ) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                self.fail(f"{value!r} is not a valid ISO-8601 timestamp", param, ctx)
        else:
            self.fail(f"{value!r} is not a timestamp", param, ctx)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            self.fail("timestamp must include a UTC offset", param, ctx)
        return parsed


_AWARE_DATETIME = _AwareDateTime()


def _ambiguity_message(error: ShadowCohortAmbiguityError) -> str:
    counts = "\n".join(f"  {value}: {count}" for value, count in sorted(error.counts.items()))
    return f"Ambiguous {error.dimension} cohorts:\n{counts}"


def _presentation_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "system_prompt": row.get("system_prompt"),
        "query_text": row.get("query_text"),
        "presented": row.get("presented"),
        "prompt_hash": row.get("prompt_hash"),
    }


def _ship_sample_payload(
    sample: ShipAuditSample,
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    row_by_target = {
        (str(row.get("recall_request_id") or ""), str(row.get("memory_id") or "")): row
        for row in rows
    }
    return [
        {
            "request_id": target.request_id,
            "memory_id": target.memory_id,
            "prompt_hash": target.prompt_hash,
            "judge_useful": target.judge_useful,
            "presentation": _presentation_payload(
                row_by_target[(target.request_id, target.memory_id)]
            ),
        }
        for target in sample.targets
    ]


def _diagnostic_sample(
    rows: Sequence[Mapping[str, Any]],
    *,
    train_request_ids: set[str],
    cohort_digest: str,
    n_requests: int,
) -> list[dict[str, Any]]:
    cells: dict[tuple[str, bool], list[Mapping[str, Any]]] = {}
    for row in rows:
        request_id = str(row.get("recall_request_id") or "")
        useful = row.get("judge_useful")
        rank = row.get("rank")
        if request_id not in train_request_ids or not isinstance(useful, bool):
            continue
        if not isinstance(rank, int):
            continue
        band = "ranks_1_4" if rank <= 4 else "ranks_5_8"
        cells.setdefault((band, useful), []).append(row)
    for candidates in cells.values():
        candidates.sort(
            key=lambda row: canonical_digest(
                {
                    "cohort_digest": cohort_digest,
                    "request_id": row.get("recall_request_id"),
                    "memory_id": row.get("memory_id"),
                }
            )
        )

    selected: list[dict[str, Any]] = []
    selected_requests: set[str] = set()
    ordered_cells = sorted(cells, key=lambda cell: (len(cells[cell]), cell))
    while len(selected) < n_requests:
        progressed = False
        for cell in ordered_cells:
            while cells[cell]:
                row = cells[cell].pop(0)
                request_id = str(row.get("recall_request_id") or "")
                if request_id in selected_requests:
                    continue
                selected.append(
                    {
                        "request_id": request_id,
                        "memory_id": str(row.get("memory_id") or ""),
                        "prompt_hash": str(row.get("prompt_hash") or ""),
                        "judge_useful": row.get("judge_useful"),
                        "diagnostic_cell": {"rank_band": cell[0], "judge_useful": cell[1]},
                        "presentation": _presentation_payload(row),
                    }
                )
                selected_requests.add(request_id)
                progressed = True
                break
            if len(selected) >= n_requests:
                break
        if not progressed:
            break
    return selected


@recall_signals.command("gate")
@click.option("--label-source", required=True)
@click.option(
    "--protocol-version",
    required=True,
    help=f"Judge protocol fence (current: {SHADOW_PROTOCOL_VERSION}).",
)
@click.option(
    "--query-construction-version",
    default=RECALL_QUERY_CONSTRUCTION_VERSION,
    show_default=True,
    help="Query-construction era fence. Legacy cohorts are frozen and superseded.",
)
@click.option("--regime-key", required=True)
@click.option("--judge-model-key", required=True)
@click.option("--judge-config-fingerprint", required=True)
@click.option("--data-cutoff", required=True, type=_AWARE_DATETIME)
@click.option("--completion-cutoff", required=True, type=_AWARE_DATETIME)
@click.option("--candidate-scope", type=click.Choice(["injected", "full"]), default=None)
@click.option(
    "--write-decision",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the complete decision record to PATH.",
)
def gate(
    label_source: str,
    protocol_version: str,
    query_construction_version: str,
    regime_key: str,
    judge_model_key: str,
    judge_config_fingerprint: str,
    data_cutoff: datetime,
    completion_cutoff: datetime,
    candidate_scope: str | None,
    write_decision: Path | None,
) -> None:
    """Run the one-shot fitted-constants ship gate over an exact cohort."""
    store = RecallSignalStore(require_cli_database())
    try:
        decision = run_ship_gate_from_store(
            store,
            label_source=label_source,
            judge_protocol_version=protocol_version,
            query_construction_version=query_construction_version,
            weighting_regime_key=regime_key,
            judge_model_key=judge_model_key,
            judge_config_fingerprint=judge_config_fingerprint,
            data_cutoff=data_cutoff,
            completion_cutoff=completion_cutoff,
            candidate_scope=candidate_scope,
        )
    except ShadowCohortAmbiguityError as error:
        raise click.ClickException(_ambiguity_message(error)) from error

    record = decision.to_record()
    serialized = json.dumps(record, indent=2, sort_keys=True)
    click.echo(serialized)
    if write_decision is not None:
        write_decision.write_text(f"{serialized}\n", encoding="utf-8")
    if not decision.ship:
        raise click.exceptions.Exit(1)


@recall_signals.command("audit-labels")
@click.option("--label-source", required=True)
@click.option(
    "--protocol-version",
    required=True,
    help=f"Judge protocol fence (current: {SHADOW_PROTOCOL_VERSION}).",
)
@click.option(
    "--query-construction-version",
    default=RECALL_QUERY_CONSTRUCTION_VERSION,
    show_default=True,
    help="Query-construction era fence. Must match the gate's for the audit to bind.",
)
@click.option("--regime-key", required=True)
@click.option("--judge-model-key", required=True)
@click.option("--judge-config-fingerprint", required=True)
@click.option("--data-cutoff", required=True, type=_AWARE_DATETIME)
@click.option("--completion-cutoff", required=True, type=_AWARE_DATETIME)
@click.option(
    "--candidate-scope",
    type=click.Choice(["injected", "full"]),
    default="full",
    show_default=True,
)
@click.option("--n-requests", type=click.IntRange(min=1), default=AUDIT_SAMPLE_REQUESTS)
@click.option("--record-agreement", is_flag=True)
@click.option("--diagnostic", is_flag=True)
@click.option("--reviewer", default=None, help="Reviewer identity stored with human verdicts.")
def audit_labels(
    label_source: str,
    protocol_version: str,
    query_construction_version: str,
    regime_key: str,
    judge_model_key: str,
    judge_config_fingerprint: str,
    data_cutoff: datetime,
    completion_cutoff: datetime,
    candidate_scope: str,
    n_requests: int,
    record_agreement: bool,
    diagnostic: bool,
    reviewer: str | None,
) -> None:
    """Print or score a deterministic snapshot-bound shadow-label audit."""
    if record_agreement and diagnostic:
        raise click.UsageError("--record-agreement and --diagnostic are mutually exclusive")
    if not diagnostic and n_requests != AUDIT_SAMPLE_REQUESTS:
        raise click.UsageError(f"ship audit requires --n-requests {AUDIT_SAMPLE_REQUESTS}")
    weighting_mode = cast(WeightingMode, candidate_scope)
    cohort = GateCohort(
        label_source=label_source,
        candidate_scope=candidate_scope,
        judge_protocol_version=protocol_version,
        query_construction_version=query_construction_version,
        weighting_regime_key=regime_key,
        judge_model_key=judge_model_key,
        judge_config_fingerprint=judge_config_fingerprint,
        data_cutoff=data_cutoff,
        completion_cutoff=completion_cutoff,
        weighting_mode=weighting_mode,
    )
    store = RecallSignalStore(require_cli_database())
    try:
        cohort_rows = store.shadow_cohort_query(
            "audit_scored",
            label_source=label_source,
            judge_protocol_version=protocol_version,
            query_construction_version=query_construction_version,
            judge_model_key=judge_model_key,
            judge_config_fingerprint=judge_config_fingerprint,
            weighting_regime_key=regime_key,
            data_cutoff=data_cutoff,
            completion_cutoff=completion_cutoff,
            limit=100_000,
        )
        train_request_ids, _ = split_request_ids_per_project(
            [
                (
                    str(row["project_id"]) if row.get("project_id") is not None else None,
                    str(row["recall_request_id"]),
                )
                for row in cohort_rows
            ]
        )
        rows = store.fetch_shadow_replay_rows(
            phase="audit_scored",
            label_source=label_source,
            candidate_scope=candidate_scope,
            judge_protocol_version=protocol_version,
            query_construction_version=query_construction_version,
            weighting_regime_key=regime_key,
            judge_model_key=judge_model_key,
            judge_config_fingerprint=judge_config_fingerprint,
            data_cutoff=data_cutoff,
            completion_cutoff=completion_cutoff,
            project_id=None,
            limit=100_000,
            request_ids=sorted(train_request_ids),
        )
    except ShadowCohortAmbiguityError as error:
        raise click.ClickException(_ambiguity_message(error)) from error

    if diagnostic:
        diagnostic_payload = {
            "mode": "diagnostic",
            "cohort": cohort.identity(),
            "cohort_digest": cohort.digest,
            "sample": _diagnostic_sample(
                rows,
                train_request_ids=train_request_ids,
                cohort_digest=cohort.digest,
                n_requests=n_requests,
            ),
        }
        click.echo(json.dumps(diagnostic_payload, indent=2, sort_keys=True))
        return

    sample = build_ship_audit_sample(
        rows,
        cohort=cohort,
        train_request_ids=train_request_ids,
    )
    sample_payload = _ship_sample_payload(sample, rows)
    ship_payload: dict[str, Any] = {
        "mode": "ship",
        "cohort": cohort.identity(),
        "cohort_digest": sample.cohort_digest,
        "sample_digest": sample.sample_digest,
        "sample": sample_payload,
    }
    if len(sample.targets) != AUDIT_SAMPLE_REQUESTS:
        ship_payload["status"] = "insufficient_training_sample"
        click.echo(json.dumps(ship_payload, indent=2, sort_keys=True))
        raise click.exceptions.Exit(1)

    if record_agreement:
        reviewer_name = reviewer or getpass.getuser()
        verdict_rows = [
            {
                "request_id": target["request_id"],
                "memory_id": target["memory_id"],
                "prompt_hash": target["prompt_hash"],
                "human_verdict": click.confirm(
                    f"{json.dumps(target['presentation'], sort_keys=True)}\nHuman verdict: relevant",
                    default=None,
                ),
                "reviewer": reviewer_name,
            }
            for target in sample_payload
        ]
        ship_payload["inserted_verdicts"] = store.insert_audit_verdicts(
            verdict_rows,
            cohort_digest=sample.cohort_digest,
            sample_digest=sample.sample_digest,
        )
        expected_hashes = {
            (target.request_id, target.memory_id): target.prompt_hash for target in sample.targets
        }
        verdicts = store.fetch_audit_verdicts(
            sample.cohort_digest,
            sample.sample_digest,
            expected_prompt_hashes=expected_hashes,
        )
        agreement = evaluate_ship_audit(sample, verdicts)
        ship_payload["agreement"] = {
            "status": agreement.status,
            "unit_count": agreement.unit_count,
            "agreement": agreement.agreement,
            "wilson_lower_bound": agreement.wilson_lower_bound,
        }
    click.echo(json.dumps(ship_payload, indent=2, sort_keys=True))


@recall_signals.command("supersede-legacy-cohort")
@click.option("--label-source", required=True)
@click.option(
    "--protocol-version",
    required=True,
    help=f"Judge protocol fence (current: {SHADOW_PROTOCOL_VERSION}).",
)
def supersede_legacy_cohort(label_source: str, protocol_version: str) -> None:
    """Retire the unjudged pre-cutover backlog for one label stream.

    Run it after the v1 poller has drained and the protocol version has been
    flipped. It is idempotent and never touches a completed label, so a repeat
    run — or a run before the flip — is safe.
    """
    store = RecallSignalStore(require_cli_database())
    superseded = store.supersede_legacy_cohort(
        label_source=label_source,
        judge_protocol_version=protocol_version,
    )
    click.echo(
        json.dumps(
            {
                "label_source": label_source,
                "judge_protocol_version": protocol_version,
                "superseded": superseded,
            },
            indent=2,
            sort_keys=True,
        )
    )


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
    from gobby.memory.recall_drift import DriftThresholds, run_drift_check_from_store

    config = get_cli_runtime(ctx).config
    memory_config = config.memory

    thresholds = None
    if threshold is not None or min_pairs is not None:
        defaults = DriftThresholds(accuracy_drop=memory_config.recall_drift_accuracy_drop)
        thresholds = DriftThresholds(
            accuracy_drop=threshold if threshold is not None else defaults.accuracy_drop,
            min_pairs=min_pairs if min_pairs is not None else defaults.min_pairs,
        )

    db = require_cli_database(ctx)
    report = run_drift_check_from_store(
        RecallSignalStore(db),
        memory_config,
        thresholds=thresholds,
    )
    click.echo(json.dumps(report.to_record(), indent=2))
    if report.alarm:
        raise SystemExit(1)


@recall_signals.command("replay-candidate-filter")
@click.option("--label-source", required=True)
@click.option(
    "--protocol-version",
    required=True,
    help=f"Judge protocol fence (current: {SHADOW_PROTOCOL_VERSION}).",
)
@click.option(
    "--query-construction-version",
    default=RECALL_QUERY_CONSTRUCTION_VERSION,
    show_default=True,
    help="Query-construction era fence. The report records the cohort it ran under.",
)
@click.option("--regime-key", required=True)
@click.option("--judge-model-key", required=True)
@click.option("--judge-config-fingerprint", required=True)
@click.option("--data-cutoff", required=True, type=_AWARE_DATETIME)
@click.option("--completion-cutoff", required=True, type=_AWARE_DATETIME)
@click.option(
    "--candidate-scope",
    type=click.Choice(["injected", "full"]),
    default="full",
    show_default=True,
)
@click.option(
    "--filter-min-score",
    type=click.FloatRange(min=0.0, max=1.0),
    default=CandidateFilterParams().min_score,
    show_default=True,
    help="Query-coverage floor the replayed candidate filter admits on.",
)
@click.option(
    "--max-selected",
    type=click.IntRange(min=1),
    default=CandidateFilterParams().max_selected,
    show_default=True,
    help="Per-request selection cap applied to both arms.",
)
@click.option(
    "--static-min-similarity",
    type=click.FloatRange(min=0.0, max=1.0),
    default=None,
    help="Static-constant arm's similarity floor (defaults to memory_recall.selection_min_score).",
)
@click.option(
    "--static-graph-confidence-min-score",
    type=click.FloatRange(min=0.0, max=1.0),
    default=_GRAPH_CONFIDENCE_SELECTION_FLOOR,
    show_default=True,
    help=(
        "Static-constant arm's entity-match confidence floor for a graph-expander "
        "find, which live selection judges on confidence rather than cosine. "
        "Provisional: the Phase 4 refit owns this constant."
    ),
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the report JSON to PATH so the numbers survive the terminal.",
)
@click.pass_context
def replay_candidate_filter_command(
    ctx: click.Context,
    label_source: str,
    protocol_version: str,
    query_construction_version: str,
    regime_key: str,
    judge_model_key: str,
    judge_config_fingerprint: str,
    data_cutoff: datetime,
    completion_cutoff: datetime,
    candidate_scope: str,
    filter_min_score: float,
    max_selected: int,
    static_min_similarity: float | None,
    static_graph_confidence_min_score: float,
    out_path: Path | None,
) -> None:
    """Replay a no-digest candidate filter against static constants on v1 labels.

    Reports request-level abstention behavior for both arms, so an arm that
    looks accurate only because it almost never selects anything cannot hide
    behind pairwise accuracy. No live-path constant changes: this is an
    offline read of an already-fenced cohort.
    """
    if static_min_similarity is None:
        static_min_similarity = get_cli_runtime(ctx).config.memory_recall.selection_min_score

    weighting_mode = cast(WeightingMode, candidate_scope)
    cohort = GateCohort(
        label_source=label_source,
        candidate_scope=candidate_scope,
        judge_protocol_version=protocol_version,
        query_construction_version=query_construction_version,
        weighting_regime_key=regime_key,
        judge_model_key=judge_model_key,
        judge_config_fingerprint=judge_config_fingerprint,
        data_cutoff=data_cutoff,
        completion_cutoff=completion_cutoff,
        weighting_mode=weighting_mode,
    )
    store = RecallSignalStore(require_cli_database(ctx))
    try:
        rows = store.fetch_shadow_replay_rows(
            phase="fitting",
            label_source=label_source,
            candidate_scope=candidate_scope,
            judge_protocol_version=protocol_version,
            query_construction_version=query_construction_version,
            weighting_regime_key=regime_key,
            judge_model_key=judge_model_key,
            judge_config_fingerprint=judge_config_fingerprint,
            data_cutoff=data_cutoff,
            completion_cutoff=completion_cutoff,
            project_id=None,
            limit=100_000,
        )
    except ShadowCohortAmbiguityError as error:
        raise click.ClickException(_ambiguity_message(error)) from error

    report = replay_candidate_filter(
        rows,
        cohort_identity=cohort.identity(),
        static_min_similarity=static_min_similarity,
        static_graph_confidence_min_score=static_graph_confidence_min_score,
        params=CandidateFilterParams(min_score=filter_min_score, max_selected=max_selected),
    )
    serialized = json.dumps(report.to_record(), indent=2, sort_keys=True)
    click.echo(serialized)
    if out_path is not None:
        out_path.write_text(f"{serialized}\n", encoding="utf-8")
