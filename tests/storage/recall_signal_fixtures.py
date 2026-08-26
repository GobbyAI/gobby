"""Shared row builders for the recall-signal hub tests.

Not a test module: :mod:`tests.storage.test_recall_signals` and
:mod:`tests.storage.test_recall_shadow_signals` both build the same promoted
signal rows, so the builders live here instead of being duplicated or imported
across test modules.
"""

from __future__ import annotations

from datetime import datetime

from gobby.storage.recall_signals import RecallSignalStore


def signal_event(request_id: str = "req-1", session_id: str = "sess-1") -> dict[str, object]:
    return {
        "schema_version": 3,
        "timestamp": "2026-07-09T12:00:00+00:00",
        "session_id": session_id,
        "recall_request_id": request_id,
        "project_id": "proj-1",
        "caller": "memory.recall",
        "query": "how does dispatch work",
        "merged_ids": ["mem-1", "mem-2"],
        "returned_ids": ["mem-1", "mem-2"],
        "rrf_applied": True,
        "graph_synthetic_similarity_discount": 0.9,
        "ranking_score_map": {"mem-1": 0.9, "mem-2": 0.4},
        "graph_score_map": {"mem-2": 0.8},
        "weighting": {"graph_edge_weighting": True},
        "hits": [
            {
                "memory_id": "mem-1",
                "rank": 0,
                "search_via": "semantic",
                "similarity": 0.9,
                "raw_semantic_score": 0.9,
                "temporal_decay_factor": 1.0,
                "ranking_score": 0.9,
                "ranking_mode": "rrf",
                "graph_score": None,
                "edge_cosine": None,
                "edge_support_norm": None,
                "edge_weight_blend": None,
                "edge_decay_factor": None,
            },
            {
                "memory_id": "mem-2",
                "rank": 1,
                "search_via": "graph",
                "similarity": 0.72,
                "raw_semantic_score": None,
                "temporal_decay_factor": 1.0,
                "ranking_score": 0.4,
                "ranking_mode": "graph_synthetic",
                "graph_score": 0.8,
                "edge_cosine": 0.8,
                "edge_support_norm": 0.6,
                "edge_weight_blend": 0.7,
                "edge_decay_factor": 1.0,
            },
        ],
    }


def shadow_event(
    request_id: str,
    *,
    session_id: str = "sess-shadow",
    query: str = "how does dispatch work",
    schema_version: int = 4,
    complete_hashes: bool = True,
    query_construction_version: str | None = None,
    caller: str = "memory.recall",
) -> dict[str, object]:
    event = signal_event(request_id=request_id, session_id=session_id)
    event["caller"] = caller
    event["schema_version"] = schema_version
    event["query"] = query
    event["constants_provenance"] = "static"
    if query_construction_version is not None:
        weighting = event["weighting"]
        assert isinstance(weighting, dict)
        weighting["query_construction_version"] = query_construction_version
    hits = event["hits"]
    assert isinstance(hits, list)
    for hit in hits:
        assert isinstance(hit, dict)
        hit["content_hash"] = f"hash-{hit['memory_id']}" if complete_hashes else None
    return event


def shadow_snapshot(request_id: str) -> dict[str, object]:
    return {
        "recall_request_id": request_id,
        "label_source": "digest_shadow",
        "judge_protocol_version": "shadow-v1",
        "system_prompt": "score query relevance",
        "query_text": "how does dispatch work",
        "presented": [
            {
                "neutral_key": "A",
                "memory_id": "mem-1",
                "order_index": 0,
                "excerpt": "dispatch uses a staged pipeline",
                "content_hash": "hash-mem-1",
            },
            {
                "neutral_key": "B",
                "memory_id": "mem-2",
                "order_index": 1,
                "excerpt": "dispatch claims tasks atomically",
                "content_hash": "hash-mem-2",
            },
        ],
        "prompt_hash": f"prompt-{request_id}",
        "judge_model": "judge-model",
        "judge_config_fingerprint": "judge-fingerprint",
        "created_at": "2026-07-17T12:30:00+00:00",
    }


def shadow_labels(request_id: str) -> list[dict[str, object]]:
    return [
        {
            "project_id": "proj-1",
            "session_id": "sess-shadow",
            "recall_request_id": request_id,
            "memory_id": memory_id,
            "label_source": "digest_shadow",
            "judge_useful": useful,
            "judge_confidence": 0.9,
            "judge_model": "judge-model",
            "judge_protocol_version": "shadow-v1",
            "position_randomized": True,
            "length_controlled": True,
            "rationale": "comparative relevance",
            "labeled_at": "2026-07-17T12:30:00+00:00",
        }
        for memory_id, useful in (("mem-1", True), ("mem-2", False))
    ]


def complete_shadow_request(
    store: RecallSignalStore,
    request_id: str,
    *,
    protocol_version: str = "shadow-v1",
    judge_model: str = "judge-model",
    judge_config_fingerprint: str = "judge-fingerprint",
    snapshot_created_at: str = "2026-07-17T12:30:00+00:00",
    query_construction_version: str | None = None,
) -> None:
    assert (
        store.insert_signal_event(
            shadow_event(request_id, query_construction_version=query_construction_version)
        )
        is True
    )
    token = store.claim_shadow_request(
        "sess-shadow",
        request_id,
        label_source="digest_shadow",
        judge_protocol_version=protocol_version,
        query_construction_version=query_construction_version,
    )
    assert token is not None
    labels = shadow_labels(request_id)
    for label in labels:
        label["judge_protocol_version"] = protocol_version
        label["judge_model"] = judge_model
    snapshot = shadow_snapshot(request_id)
    snapshot["judge_protocol_version"] = protocol_version
    snapshot["judge_model"] = judge_model
    snapshot["judge_config_fingerprint"] = judge_config_fingerprint
    assert store.insert_usefulness_labels_atomic(labels, snapshot, token) is True
    # Snapshot completion time is DB-owned (DEFAULT now()); backdate it so the
    # cutoff-based cohort assertions stay deterministic.
    store.db.execute(
        "UPDATE recall_shadow_prompt_snapshot SET created_at = %s "
        "WHERE recall_request_id = %s AND judge_protocol_version = %s",
        (datetime.fromisoformat(snapshot_created_at), request_id, protocol_version),
    )
