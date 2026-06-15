"""Tests for observational recall/search signal JSONL logging."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.recall_signal_log import (
    append_recall_signal_events,
    build_recall_signal_event,
    default_recall_signal_path,
    make_recall_signal_sink,
    resolve_recall_signal_path,
)
from gobby.memory.services.search import SearchDebugHit, SearchDebugSnapshot


def _snapshot() -> SearchDebugSnapshot:
    return SearchDebugSnapshot(
        merged_ids=["semantic", "graph"],
        returned_ids=["semantic", "graph"],
        ranking_score_map={"semantic": 0.9, "graph": float("inf")},
        rrf_applied=True,
        query="raw user query",
        project_id="project-1",
        session_id="session-1",
        recall_request_id="request-1",
        caller="memory.recall",
        graph_score_map={"graph": 0.8, "bad": float("nan")},
        returned_hits=[
            SearchDebugHit(
                memory_id="semantic",
                rank=0,
                search_via="semantic",
                similarity=0.9,
                raw_semantic_score=0.9,
                temporal_decay_factor=1.0,
                ranking_score=0.9,
                ranking_mode="rrf",
                graph_score=None,
            ),
            SearchDebugHit(
                memory_id="graph",
                rank=1,
                search_via="graph",
                similarity=0.72,
                raw_semantic_score=None,
                temporal_decay_factor=1.0,
                ranking_score=float("inf"),
                ranking_mode="graph_synthetic",
                graph_score=0.8,
            ),
        ],
    )


def test_resolve_recall_signal_path_defaults_and_expands_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))

    assert default_recall_signal_path() == tmp_path / "home" / "logs" / "recall_signal.jsonl"
    assert resolve_recall_signal_path(None) == default_recall_signal_path()
    assert str(resolve_recall_signal_path("~/signals.jsonl")).endswith("/signals.jsonl")


def test_build_recall_signal_event_records_search_features_and_sanitizes_floats() -> None:
    event = build_recall_signal_event(
        snapshot=_snapshot(),
        timestamp="2026-06-15T00:00:00+00:00",
        weighting={
            "graph_edge_weighting": False,
            "graph_edge_decay": False,
            "edge_half_life_days": 30.0,
            "materialize_cooccurrence": False,
            "cluster_recall_expansion": False,
            "cluster_expansion_per_entity": 3,
            "cluster_min_cluster_size": 5,
            "cluster_min_samples": 2,
            "temporal_decay_half_life_days": 30.0,
        },
    )

    assert event["schema_version"] == 2
    assert event["timestamp"] == "2026-06-15T00:00:00+00:00"
    assert event["project_id"] == "project-1"
    assert event["session_id"] == "session-1"
    assert event["recall_request_id"] == "request-1"
    assert event["caller"] == "memory.recall"
    assert event["query"] == "raw user query"
    assert event["merged_ids"] == ["semantic", "graph"]
    assert event["returned_ids"] == ["semantic", "graph"]
    assert event["rrf_applied"] is True
    assert event["graph_synthetic_similarity_discount"] == 0.9
    assert event["ranking_score_map"] == {"semantic": 0.9, "graph": None}
    assert event["graph_score_map"] == {"graph": 0.8, "bad": None}
    assert event["hits"][1]["ranking_mode"] == "graph_synthetic"
    assert event["hits"][1]["graph_score"] == 0.8
    assert event["hits"][1]["ranking_score"] is None
    json.dumps(event, allow_nan=False)


def test_build_recall_signal_event_includes_join_metadata_when_present() -> None:
    event = build_recall_signal_event(
        snapshot=SearchDebugSnapshot(
            merged_ids=[],
            returned_ids=[],
            ranking_score_map={},
            rrf_applied=False,
            session_id="session-1",
            recall_request_id="request-1",
            caller="memory.recall",
        ),
        timestamp="2026-06-15T00:00:00+00:00",
        weighting={},
    )

    assert event["session_id"] == "session-1"
    assert event["recall_request_id"] == "request-1"
    assert event["caller"] == "memory.recall"


def test_build_recall_signal_event_defaults_join_metadata_for_non_recall() -> None:
    event = build_recall_signal_event(
        snapshot=SearchDebugSnapshot(
            merged_ids=[],
            returned_ids=[],
            ranking_score_map={},
            rrf_applied=False,
        ),
        timestamp="2026-06-15T00:00:00+00:00",
        weighting={},
    )

    assert event["session_id"] is None
    assert event["recall_request_id"] is None
    assert event["caller"] == "memory.search"


def test_append_recall_signal_events_writes_parseable_jsonl_and_appends(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "recall_signal.jsonl"
    event = build_recall_signal_event(
        snapshot=_snapshot(),
        timestamp="2026-06-15T00:00:00+00:00",
        weighting={},
    )

    append_recall_signal_events([], path)
    assert not path.exists()

    append_recall_signal_events([event], path)
    append_recall_signal_events([event], path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["query"] for line in lines] == ["raw user query", "raw user query"]


def test_append_recall_signal_events_fails_open(tmp_path: Path) -> None:
    event = build_recall_signal_event(
        snapshot=_snapshot(),
        timestamp="2026-06-15T00:00:00+00:00",
        weighting={},
    )

    append_recall_signal_events([event], tmp_path)
    assert tmp_path.is_dir()


def test_make_recall_signal_sink_is_default_off_and_writes_when_enabled(tmp_path: Path) -> None:
    assert make_recall_signal_sink(MemoryConfig()) is None

    path = tmp_path / "recall_signal.jsonl"
    sink = make_recall_signal_sink(
        MemoryConfig(
            recall_signal_logging=True,
            recall_signal_log_path=str(path),
            graph_edge_weighting=True,
            cluster_recall_expansion=True,
            cluster_expansion_per_entity=7,
            cluster_min_cluster_size=8,
            cluster_min_samples=None,
        )
    )
    assert sink is not None

    sink(_snapshot())

    [line] = path.read_text(encoding="utf-8").splitlines()
    event = json.loads(line)
    assert event["query"] == "raw user query"
    assert event["weighting"]["graph_edge_weighting"] is True
    assert event["weighting"]["cluster_recall_expansion"] is True
    assert event["weighting"]["cluster_expansion_per_entity"] == 7
    assert event["weighting"]["cluster_min_cluster_size"] == 8
    assert event["weighting"]["cluster_min_samples"] is None
