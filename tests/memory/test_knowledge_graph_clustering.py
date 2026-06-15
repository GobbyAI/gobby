"""Unit tests for offline KG entity clustering."""

from __future__ import annotations

import builtins
from typing import Any

import pytest

from gobby.memory.services.knowledge_graph.clustering import (
    EntityVector,
    _canonicalize_labels,
    _coerce_unit_vector,
    _quality_metrics,
    cluster_entity_vectors,
)

pytestmark = pytest.mark.unit


def _entity(entity_key: str, embedding: Any) -> EntityVector:
    return EntityVector(entity_key=entity_key, name=entity_key, embedding=embedding)


def test_canonicalize_labels_orders_clusters_by_member_keys() -> None:
    labels = _canonicalize_labels(
        ["b", "a", "c", "noise", "d"],
        [7, 3, 7, -1, 3],
    )

    assert labels == {
        "a": 0,
        "d": 0,
        "b": 1,
        "c": 1,
        "noise": None,
    }


def test_unit_normalization_preserves_cosine_neighborhoods() -> None:
    assert _coerce_unit_vector([2.0, 0.0]) == [1.0, 0.0]
    assert _coerce_unit_vector([0.0, -4.0]) == [0.0, -1.0]


def test_cluster_entity_vectors_finds_planted_direction_clusters() -> None:
    entities = [
        _entity("alpha-1", [1.0, 0.0]),
        _entity("alpha-2", [0.99, 0.01]),
        _entity("alpha-3", [0.98, -0.02]),
        _entity("beta-1", [-1.0, 0.0]),
        _entity("beta-2", [-0.99, 0.01]),
        _entity("beta-3", [-0.98, -0.02]),
    ]

    result = cluster_entity_vectors(entities, project_id="project-1")

    assert result.project_id == "project-1"
    assert result.cluster_count == 2
    assert result.cluster_sizes == {0: 3, 1: 3}
    assert result.noise_count == 0
    assert result.invalid_count == 0
    assert result.quality_metrics["silhouette"] is not None


def test_invalid_zero_and_dimension_mismatch_vectors_are_cluster_clear_targets() -> None:
    entities = [
        _entity("valid-a", [1.0, 0.0]),
        _entity("valid-b", [0.0, 1.0]),
        _entity("zero", [0.0, 0.0]),
        _entity("nan", [float("nan"), 1.0]),
        _entity("text", ["bad"]),
        _entity("wrong-dim", [1.0, 0.0, 0.0]),
    ]

    result = cluster_entity_vectors(entities, project_id=None)

    assert result.cluster_count == 0
    assert result.noise_count == 2
    assert result.invalid_count == 4
    assert result.invalid_entity_keys == ["nan", "text", "wrong-dim", "zero"]
    assert set(result.cluster_ids_by_entity_key.values()) == {None}


def test_small_valid_set_marks_noise_without_loading_sklearn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("sklearn"):
            raise AssertionError("sklearn should not load for undersized inputs")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    result = cluster_entity_vectors(
        [_entity("a", [1.0, 0.0]), _entity("b", [0.0, 1.0])],
        project_id=None,
    )

    assert result.noise_count == 2
    assert result.cluster_count == 0


def test_missing_sklearn_error_is_clear_when_clustering_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("sklearn"):
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    with pytest.raises(ImportError, match="scikit-learn>=1.5 is required"):
        cluster_entity_vectors(
            [
                _entity("a", [1.0, 0.0]),
                _entity("b", [0.9, 0.1]),
                _entity("c", [0.8, 0.2]),
            ],
            project_id=None,
        )


def test_quality_metrics_omit_undefined_silhouette_for_single_cluster() -> None:
    metrics = _quality_metrics(
        matrix=None,
        labels_by_key={"a": 0, "b": 0, "c": 0},
        entity_keys=["a", "b", "c"],
        silhouette_score=None,
        entity_count=3,
        invalid_count=0,
    )

    assert metrics["silhouette"] is None
    assert metrics["clustered_ratio"] == 1.0
