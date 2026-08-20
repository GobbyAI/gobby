"""Tests for observational recall/search signal JSONL logging."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.recall_signal_log import (
    append_recall_signal_events,
    build_recall_signal_event,
    default_recall_signal_path,
    make_injection_outcome_recorder,
    make_recall_signal_sink,
    resolve_recall_signal_path,
)
from gobby.memory.services._search_debug import emit_search_debug
from gobby.memory.services.search import SearchDebugHit, SearchDebugSnapshot
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories_models import Memory, MemoryType


class _FakeCursor:
    rowcount = 1


class _FakeTxn:
    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self._calls = calls

    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        self._calls.append((sql, params))
        return _FakeCursor()


class _FakeHubDb:
    """Minimal HubDatabase stand-in recording executed SQL."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    @contextmanager
    def transaction(self) -> Iterator[_FakeTxn]:
        yield _FakeTxn(self.calls)

    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        self.calls.append((sql, params))
        return _FakeCursor()


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
        constants_provenance="static",
        graph_score_map={"graph": 0.8, "bad": float("nan")},
        graph_component_map={
            "graph": {
                "edge_cosine": 0.8,
                "edge_support_norm": 0.6,
                "edge_weight_blend": 0.7,
                "edge_decay_factor": 1.0,
            }
        },
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
                content_hash="semantic-hash",
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
                content_hash="graph-hash",
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

    assert event["schema_version"] == 4
    assert event["timestamp"] == "2026-06-15T00:00:00+00:00"
    assert event["project_id"] == "project-1"
    assert event["session_id"] == "session-1"
    assert event["recall_request_id"] == "request-1"
    assert event["caller"] == "memory.recall"
    assert event["constants_provenance"] == "static"
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
    assert event["hits"][0]["content_hash"] == "semantic-hash"
    assert event["hits"][1]["content_hash"] == "graph-hash"
    # §3.2 edge-component breakdown: present for the traversal-admitted hit,
    # None for hits that entered through other paths.
    assert event["hits"][1]["edge_cosine"] == 0.8
    assert event["hits"][1]["edge_support_norm"] == 0.6
    assert event["hits"][1]["edge_weight_blend"] == 0.7
    assert event["hits"][1]["edge_decay_factor"] == 1.0
    assert event["hits"][0]["edge_cosine"] is None
    assert event["hits"][0]["edge_support_norm"] is None
    assert event["hits"][0]["edge_weight_blend"] is None
    assert event["hits"][0]["edge_decay_factor"] is None
    json.dumps(event, allow_nan=False)


def test_emit_search_debug_stamps_exact_content_identity_through_hub_write(
    tmp_path: Path,
) -> None:
    db = _FakeHubDb()
    hub_sink = make_recall_signal_sink(
        MemoryConfig(recall_signal_hub=True, recall_signal_log_path=str(tmp_path / "signals")),
        cast(HubDatabase, db),
    )
    assert hub_sink is not None
    snapshots: list[SearchDebugSnapshot] = []

    def capture(snapshot: SearchDebugSnapshot) -> None:
        snapshots.append(snapshot)
        hub_sink(snapshot)

    now = datetime.now(UTC)
    content = "café\n未尾"
    emit_search_debug(
        search_debug_sink=capture,
        query="unicode identity",
        project_id="project-1",
        session_id="session-1",
        recall_request_id="request-content-hash",
        caller="memory.recall",
        merged_ids=["memory-1"],
        returned=[
            Memory(
                id="memory-1",
                memory_type=MemoryType.FACT,
                content=content,
                created_at=now,
                updated_at=now,
            )
        ],
        ranking_score_map={"memory-1": 0.7},
        rrf_applied=False,
        constants_provenance="decision-digest-123",
    )

    expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert snapshots[0].constants_provenance == "decision-digest-123"
    assert snapshots[0].returned_hits[0].content_hash == expected_hash

    request_sql, request_params = next(
        call for call in db.calls if "INSERT INTO recall_signal_requests" in call[0]
    )
    hit_sql, hit_params = next(
        call for call in db.calls if "INSERT INTO recall_signal_hits" in call[0]
    )
    assert "constants_provenance" in request_sql
    assert "decision-digest-123" in request_params
    assert "content_hash" in hit_sql
    assert expected_hash in hit_params


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


def test_append_rotates_at_cap_and_shifts_backups(tmp_path: Path) -> None:
    path = tmp_path / "recall_signal.jsonl"
    event = build_recall_signal_event(
        snapshot=_snapshot(),
        timestamp="2026-06-15T00:00:00+00:00",
        weighting={},
    )
    backup_one = tmp_path / "recall_signal.jsonl.1"
    backup_two = tmp_path / "recall_signal.jsonl.2"

    path.write_text("live-generation-1\n", encoding="utf-8")
    backup_one.write_text("backup-generation-0\n", encoding="utf-8")

    append_recall_signal_events([event], path, max_bytes=1)

    assert backup_two.read_text(encoding="utf-8") == "backup-generation-0\n"
    assert backup_one.read_text(encoding="utf-8") == "live-generation-1\n"
    [line] = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["query"] == "raw user query"


def test_append_does_not_rotate_under_cap(tmp_path: Path) -> None:
    path = tmp_path / "recall_signal.jsonl"
    event = build_recall_signal_event(
        snapshot=_snapshot(),
        timestamp="2026-06-15T00:00:00+00:00",
        weighting={},
    )

    append_recall_signal_events([event], path, max_bytes=1024 * 1024)
    append_recall_signal_events([event], path, max_bytes=1024 * 1024)

    assert not (tmp_path / "recall_signal.jsonl.1").exists()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_sink_threads_configured_rotation_cap(tmp_path: Path) -> None:
    path = tmp_path / "recall_signal.jsonl"
    path.write_text("x" * (1024 * 1024) + "\n", encoding="utf-8")
    sink = make_recall_signal_sink(
        MemoryConfig(
            recall_signal_logging=True,
            recall_signal_log_path=str(path),
            recall_signal_log_max_mb=1,
        )
    )
    assert sink is not None

    sink(_snapshot())

    assert (tmp_path / "recall_signal.jsonl.1").exists()
    [line] = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["query"] == "raw user query"


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


def test_make_recall_signal_sink_hub_dual_write(tmp_path: Path) -> None:
    """recall_signal_hub alone enables the sink and writes hub rows, not JSONL."""
    # Hub flag without a db stays off.
    assert make_recall_signal_sink(MemoryConfig(recall_signal_hub=True)) is None

    db = _FakeHubDb()
    path = tmp_path / "recall_signal.jsonl"
    sink = make_recall_signal_sink(
        MemoryConfig(recall_signal_hub=True, recall_signal_log_path=str(path)),
        cast(HubDatabase, db),
    )
    assert sink is not None

    sink(_snapshot())

    assert not path.exists()
    request_inserts = [sql for sql, _ in db.calls if "recall_signal_requests" in sql]
    hit_inserts = [sql for sql, _ in db.calls if "recall_signal_hits" in sql]
    assert len(request_inserts) == 1
    assert len(hit_inserts) == 2


def test_make_injection_outcome_recorder_default_off_and_records() -> None:
    db = _FakeHubDb()
    hub_db = cast(HubDatabase, db)
    assert make_injection_outcome_recorder(MemoryConfig(), hub_db) is None
    assert make_injection_outcome_recorder(MemoryConfig(recall_signal_hub=True)) is None

    recorder = make_injection_outcome_recorder(MemoryConfig(recall_signal_hub=True), hub_db)
    assert recorder is not None

    recorder(
        [
            {
                "session_id": "session-1",
                "recall_request_id": "request-1",
                "memory_id": "memory-1",
                "outcome": "injected",
                "injection_position": 0,
                "caller": "memory.recall",
            }
        ]
    )

    outcome_inserts = [sql for sql, _ in db.calls if "recall_injection_outcomes" in sql]
    assert len(outcome_inserts) == 1


def test_hit_event_logs_rationale_hash() -> None:
    rationale = "keep the TS convention for future sessions"
    rationale_hash = hashlib.sha256(rationale.encode("utf-8")).hexdigest()
    populated = SearchDebugHit(
        memory_id="semantic",
        rank=0,
        search_via="semantic",
        similarity=0.9,
        raw_semantic_score=0.9,
        temporal_decay_factor=1.0,
        ranking_score=0.9,
        ranking_mode="rrf",
        graph_score=None,
        content_hash="semantic-hash",
        rationale_hash=rationale_hash,
    )
    legacy = SearchDebugHit(
        memory_id="graph",
        rank=1,
        search_via="graph",
        similarity=0.72,
        raw_semantic_score=None,
        temporal_decay_factor=1.0,
        ranking_score=0.8,
        ranking_mode="graph_synthetic",
        graph_score=0.8,
        content_hash="graph-hash",
    )
    snapshot = SearchDebugSnapshot(
        merged_ids=["semantic", "graph"],
        returned_ids=["semantic", "graph"],
        ranking_score_map={"semantic": 0.9, "graph": 0.8},
        rrf_applied=True,
        query="raw user query",
        project_id="project-1",
        session_id="session-1",
        recall_request_id="request-1",
        caller="memory.recall",
        constants_provenance="static",
        returned_hits=[populated, legacy],
    )
    event = build_recall_signal_event(
        snapshot=snapshot,
        timestamp="2026-06-15T00:00:00+00:00",
        weighting={},
    )
    assert event["hits"][0]["rationale_hash"] == rationale_hash
    assert "rationale_hash" in event["hits"][1]
    assert event["hits"][1]["rationale_hash"] is None
    assert "rationale" not in event["hits"][0]

    snapshots: list[SearchDebugSnapshot] = []
    now = datetime.now(UTC)
    emit_search_debug(
        search_debug_sink=snapshots.append,
        query="rationale identity",
        project_id="project-1",
        session_id="session-1",
        recall_request_id="request-rationale",
        caller="memory.recall",
        merged_ids=["memory-1", "memory-2"],
        returned=[
            Memory(
                id="memory-1",
                memory_type=MemoryType.FACT,
                content="Use TypeScript",
                created_at=now,
                updated_at=now,
                rationale="keep the TS convention for future sessions",
            ),
            Memory(
                id="memory-2",
                memory_type=MemoryType.FACT,
                content="Legacy row",
                created_at=now,
                updated_at=now,
            ),
        ],
        ranking_score_map={"memory-1": 0.7, "memory-2": 0.6},
        rrf_applied=False,
        constants_provenance="static",
    )
    assert snapshots[0].returned_hits[0].rationale_hash == rationale_hash
    assert snapshots[0].returned_hits[1].rationale_hash is None
    assert snapshots[0].returned_hits[0].rationale_hash != rationale
