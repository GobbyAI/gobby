"""Append-only observational recall/search signal logging."""

from __future__ import annotations

import json
import logging
import math
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.paths import get_gobby_home

if TYPE_CHECKING:
    from gobby.config.persistence import MemoryConfig
    from gobby.memory.services.search import SearchDebugHit, SearchDebugSnapshot

logger = logging.getLogger(__name__)

RECALL_SIGNAL_SCHEMA_VERSION = 2

_WRITE_LOCK = threading.Lock()


def default_recall_signal_path() -> Path:
    """Return the default dedicated JSONL path for recall/search signal events."""
    return get_gobby_home() / "logs" / "recall_signal.jsonl"


def resolve_recall_signal_path(path: str | None) -> Path:
    """Resolve a configured recall-signal log path."""
    if path is None:
        return default_recall_signal_path()
    return Path(path).expanduser()


def make_recall_signal_sink(config: MemoryConfig) -> Callable[[SearchDebugSnapshot], None] | None:
    """Build the default-off SearchService debug sink for recall signal logging."""
    if not getattr(config, "recall_signal_logging", False):
        return None

    path = resolve_recall_signal_path(getattr(config, "recall_signal_log_path", None))

    def sink(snapshot: SearchDebugSnapshot) -> None:
        event = build_recall_signal_event(
            snapshot=snapshot,
            timestamp=datetime.now(UTC).isoformat(),
            weighting=_weighting_snapshot(config),
        )
        append_recall_signal_events([event], path)

    return sink


def build_recall_signal_event(
    *,
    snapshot: SearchDebugSnapshot,
    timestamp: str,
    weighting: Mapping[str, object],
) -> dict[str, Any]:
    """Build one deterministic JSON-serializable event for a completed search."""
    graph_score_map = _sanitize_float_map(snapshot.graph_score_map)
    return {
        "schema_version": RECALL_SIGNAL_SCHEMA_VERSION,
        "timestamp": timestamp,
        "project_id": snapshot.project_id,
        "session_id": snapshot.session_id,
        "recall_request_id": snapshot.recall_request_id,
        "caller": snapshot.caller,
        "query": snapshot.query,
        "merged_ids": list(snapshot.merged_ids),
        "returned_ids": list(snapshot.returned_ids),
        "rrf_applied": snapshot.rrf_applied,
        "graph_synthetic_similarity_discount": _finite_or_none(
            snapshot.graph_synthetic_similarity_discount
        ),
        "ranking_score_map": _sanitize_float_map(snapshot.ranking_score_map),
        "graph_score_map": graph_score_map,
        "weighting": dict(weighting),
        "hits": [
            _hit_to_event(hit=hit, graph_score_map=graph_score_map)
            for hit in snapshot.returned_hits
        ],
    }


def append_recall_signal_events(events: list[dict[str, Any]], path: Path) -> None:
    """Append events as parseable JSONL; fail open on all filesystem/encoding errors."""
    if not events:
        return

    try:
        with _WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = "".join(
                json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n" for event in events
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write(payload)
    except Exception:
        logger.debug("Recall signal log append failed", exc_info=True)


def _weighting_snapshot(config: MemoryConfig) -> dict[str, object]:
    return {
        "graph_edge_weighting": getattr(config, "graph_edge_weighting", False),
        "graph_edge_decay": getattr(config, "graph_edge_decay", False),
        "edge_half_life_days": _finite_or_none(getattr(config, "edge_half_life_days", None)),
        "materialize_cooccurrence": getattr(config, "materialize_cooccurrence", False),
        "cluster_recall_expansion": getattr(config, "cluster_recall_expansion", False),
        "cluster_expansion_per_entity": getattr(config, "cluster_expansion_per_entity", 3),
        "temporal_decay_half_life_days": _finite_or_none(
            getattr(config, "temporal_decay_half_life_days", None)
        ),
    }


def _hit_to_event(
    *,
    hit: SearchDebugHit,
    graph_score_map: Mapping[str, float | None],
) -> dict[str, Any]:
    graph_score = graph_score_map.get(hit.memory_id)
    return {
        "memory_id": hit.memory_id,
        "rank": hit.rank,
        "search_via": hit.search_via,
        "similarity": _finite_or_none(hit.similarity),
        "raw_semantic_score": _finite_or_none(hit.raw_semantic_score),
        "temporal_decay_factor": _finite_or_none(hit.temporal_decay_factor),
        "ranking_score": _finite_or_none(hit.ranking_score),
        "ranking_mode": hit.ranking_mode,
        "graph_score": graph_score,
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
