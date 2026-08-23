"""Append-only observational recall/search signal logging."""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.memory.recall_constants import RECALL_QUERY_CONSTRUCTION_VERSION
from gobby.paths import get_gobby_home

if TYPE_CHECKING:
    from gobby.config.persistence import MemoryConfig
    from gobby.memory.recall_constants import RecallConstants
    from gobby.memory.services.search import SearchDebugHit, SearchDebugSnapshot
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

RECALL_SIGNAL_SCHEMA_VERSION = 4

_WRITE_LOCK = threading.Lock()


def default_recall_signal_path() -> Path:
    """Return the default dedicated JSONL path for recall/search signal events."""
    return get_gobby_home() / "logs" / "recall_signal.jsonl"


def resolve_recall_signal_path(path: str | None) -> Path:
    """Resolve a configured recall-signal log path."""
    if path is None:
        return default_recall_signal_path()
    return Path(path).expanduser()


def make_recall_signal_sink(
    config: MemoryConfig,
    db: HubDatabase | None = None,
    recall_constants: RecallConstants | None = None,
) -> Callable[[SearchDebugSnapshot], None] | None:
    """Build the default-off SearchService debug sink for recall signal logging.

    JSONL append is gated by ``recall_signal_logging``; the Postgres hub
    dual-write (#17196) is gated by ``recall_signal_hub`` and requires ``db``.
    Both writes fail open — signal capture never disturbs search.
    ``recall_constants`` carries the effective #17200 ranking constants so
    logged events replay against what search actually used.
    """
    jsonl_enabled = getattr(config, "recall_signal_logging", False)
    hub_db = db if getattr(config, "recall_signal_hub", False) else None
    if not jsonl_enabled and hub_db is None:
        return None

    path = resolve_recall_signal_path(getattr(config, "recall_signal_log_path", None))
    max_bytes = int(getattr(config, "recall_signal_log_max_mb", 50)) * 1024 * 1024

    def sink(snapshot: SearchDebugSnapshot) -> None:
        event = build_recall_signal_event(
            snapshot=snapshot,
            timestamp=datetime.now(UTC).isoformat(),
            weighting=_weighting_snapshot(config, recall_constants),
        )
        if jsonl_enabled:
            append_recall_signal_events([event], path, max_bytes=max_bytes)
        if hub_db is not None:
            try:
                from gobby.storage.recall_signals import RecallSignalStore

                RecallSignalStore(hub_db).insert_signal_event(event)
            except Exception:
                logger.debug("Recall signal hub dual-write failed", exc_info=True)

    return sink


def make_injection_outcome_recorder(
    config: MemoryConfig,
    db: HubDatabase | None = None,
) -> Callable[[list[dict[str, Any]]], None] | None:
    """Build the default-off durable injection-outcome writer (contract §5).

    Gated by ``recall_signal_hub`` and requires ``db``. The returned callable
    accepts ``recall_injection_outcomes`` row dicts and fails open — outcome
    capture never disturbs delivery.
    """
    hub_db = db if getattr(config, "recall_signal_hub", False) else None
    if hub_db is None:
        return None

    def record(rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        try:
            from gobby.storage.recall_signals import RecallSignalStore

            RecallSignalStore(hub_db).record_injection_outcomes(rows)
        except Exception:
            logger.debug("Injection-outcome hub write failed", exc_info=True)

    return record


def build_recall_signal_event(
    *,
    snapshot: SearchDebugSnapshot,
    timestamp: str,
    weighting: Mapping[str, object],
) -> dict[str, Any]:
    """Build one deterministic JSON-serializable event for a completed search.

    ``query`` is the text retrieval was driven by, because the shadow judge renders
    it as the user's question. When the search split that from the BM25 term bag,
    the term bag rides in ``weighting`` as ``bm25_query`` so a hybrid replay can
    reproduce both legs; ``weighting`` is otherwise a pure constants snapshot.
    """
    graph_score_map = _sanitize_float_map(snapshot.graph_score_map)
    weighting_snapshot = dict(weighting)
    if snapshot.bm25_query:
        weighting_snapshot["bm25_query"] = snapshot.bm25_query
    return {
        "schema_version": RECALL_SIGNAL_SCHEMA_VERSION,
        "timestamp": timestamp,
        "project_id": snapshot.project_id,
        "session_id": snapshot.session_id,
        "recall_request_id": snapshot.recall_request_id,
        "caller": snapshot.caller,
        "constants_provenance": snapshot.constants_provenance,
        "query": snapshot.query,
        "merged_ids": list(snapshot.merged_ids),
        "returned_ids": list(snapshot.returned_ids),
        "rrf_applied": snapshot.rrf_applied,
        "graph_synthetic_similarity_discount": _finite_or_none(
            snapshot.graph_synthetic_similarity_discount
        ),
        "ranking_score_map": _sanitize_float_map(snapshot.ranking_score_map),
        "graph_score_map": graph_score_map,
        "weighting": weighting_snapshot,
        "hits": [
            _hit_to_event(
                hit=hit,
                graph_score_map=graph_score_map,
                graph_component_map=snapshot.graph_component_map,
            )
            for hit in snapshot.returned_hits
        ],
    }


def append_recall_signal_events(
    events: list[dict[str, Any]],
    path: Path,
    max_bytes: int | None = None,
) -> None:
    """Append events as parseable JSONL; fail open on all filesystem/encoding errors.

    When ``max_bytes`` is set and the live file has reached it, the file rotates
    first (``.1`` shifts to ``.2``, live becomes ``.1``) so retained size stays
    bounded at roughly three times the cap.
    """
    if not events:
        return

    try:
        with _WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if max_bytes is not None:
                _rotate_if_needed(path, max_bytes)
            payload = "".join(
                json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n" for event in events
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write(payload)
    except Exception:
        logger.debug("Recall signal log append failed", exc_info=True)


def rotated_recall_signal_paths(path: Path) -> list[Path]:
    """Return existing log files for ``path`` oldest-first (``.2``, ``.1``, live)."""
    candidates = (
        path.with_name(path.name + ".2"),
        path.with_name(path.name + ".1"),
        path,
    )
    return [candidate for candidate in candidates if candidate.exists()]


def _rotate_if_needed(path: Path, max_bytes: int) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < max_bytes:
        return
    backup_one = path.with_name(path.name + ".1")
    backup_two = path.with_name(path.name + ".2")
    if backup_one.exists():
        os.replace(backup_one, backup_two)
    os.replace(path, backup_one)


def _weighting_snapshot(
    config: MemoryConfig, recall_constants: RecallConstants | None = None
) -> dict[str, object]:
    # Effective half-life: the #17200 resolved value when provided (fitted
    # constants may differ from the configured field), else the config read.
    if recall_constants is not None:
        half_life: object = _finite_or_none(recall_constants.half_life_days)
    else:
        half_life = _finite_or_none(getattr(config, "temporal_decay_half_life_days", None))
    snapshot: dict[str, object] = {
        # The era this request's query was built in. Rows written before the key
        # existed carry none, which is exactly how the legacy cohort is selected.
        "query_construction_version": RECALL_QUERY_CONSTRUCTION_VERSION,
        "graph_edge_weighting": getattr(config, "graph_edge_weighting", False),
        "graph_edge_decay": getattr(config, "graph_edge_decay", False),
        "edge_half_life_days": _finite_or_none(getattr(config, "edge_half_life_days", None)),
        "materialize_cooccurrence": getattr(config, "materialize_cooccurrence", False),
        "cluster_recall_expansion": getattr(config, "cluster_recall_expansion", False),
        "cluster_expansion_per_entity": getattr(config, "cluster_expansion_per_entity", 3),
        "cluster_min_cluster_size": getattr(config, "cluster_min_cluster_size", 5),
        "cluster_min_samples": getattr(config, "cluster_min_samples", 2),
        "temporal_decay_half_life_days": half_life,
    }
    if recall_constants is not None:
        snapshot["recall_constants_source"] = recall_constants.source
        snapshot["cooccur_alpha"] = _finite_or_none(recall_constants.cooccur_alpha)
        snapshot["cooccur_support_cap"] = recall_constants.cooccur_support_cap
    return snapshot


def _hit_to_event(
    *,
    hit: SearchDebugHit,
    graph_score_map: Mapping[str, float | None],
    graph_component_map: Mapping[str, Mapping[str, float | None]] | None = None,
) -> dict[str, Any]:
    graph_score = graph_score_map.get(hit.memory_id)
    # Edge-weight component breakdown (contract §3.2): present only for hits
    # that entered via weighted graph traversal; None otherwise.
    components = (graph_component_map or {}).get(hit.memory_id) or {}
    return {
        "memory_id": hit.memory_id,
        "content_hash": hit.content_hash,
        "rank": hit.rank,
        "search_via": hit.search_via,
        "similarity": _finite_or_none(hit.similarity),
        "raw_semantic_score": _finite_or_none(hit.raw_semantic_score),
        "temporal_decay_factor": _finite_or_none(hit.temporal_decay_factor),
        "ranking_score": _finite_or_none(hit.ranking_score),
        "ranking_mode": hit.ranking_mode,
        "graph_score": graph_score,
        "edge_cosine": _finite_or_none(components.get("edge_cosine")),
        "edge_support_norm": _finite_or_none(components.get("edge_support_norm")),
        "edge_weight_blend": _finite_or_none(components.get("edge_weight_blend")),
        "edge_decay_factor": _finite_or_none(components.get("edge_decay_factor")),
        "rationale_hash": hit.rationale_hash,
    }


def _sanitize_float_map(values: Mapping[str, float]) -> dict[str, float | None]:
    return {key: _finite_or_none(value) for key, value in values.items()}


def _finite_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    return numeric
