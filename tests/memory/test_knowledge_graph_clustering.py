"""Unit tests for offline KG entity clustering."""

from __future__ import annotations

import builtins
from typing import Any

import pytest

from gobby.memory.services.knowledge_graph import clustering as clustering_mod
from gobby.memory.services.knowledge_graph.clustering import (
    ClusterRunResult,
    EntityVector,
    _canonicalize_labels,
    _coerce_unit_vector,
    _quality_metrics,
    cluster_entity_vectors,
)
from tests.memory._recall_corpus import (
    DIM,
    direct_clustering_vectors,
    ground_truth_entity_clusters,
)

pytestmark = pytest.mark.unit


def _entity(entity_key: str, embedding: Any) -> EntityVector:
    return EntityVector(entity_key=entity_key, name=entity_key, embedding=embedding)


def _quality_labels(
    vectors: list[EntityVector],
    result: ClusterRunResult,
) -> tuple[list[int], list[int]]:
    truth_by_name = ground_truth_entity_clusters()
    truth: list[int] = []
    predicted: list[int] = []

    for vector in vectors:
        truth_label = truth_by_name[vector.name]
        cluster_id = result.cluster_ids_by_entity_key[vector.entity_key]
        truth.append(-1 if truth_label is None else truth_label)
        predicted.append(-1 if cluster_id is None else cluster_id)

    return truth, predicted


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

    result = cluster_entity_vectors(
        entities,
        project_id="project-1",
        min_cluster_size=3,
    )

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
            min_cluster_size=3,
        )


def test_cluster_entity_vectors_preserves_explicit_none_min_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeNumpy:
        @staticmethod
        def asarray(values: list[list[float]], dtype: type[float]) -> list[list[float]]:
            captured["dtype"] = dtype
            return values

    class FakeHDBSCAN:
        def __init__(
            self,
            *,
            min_cluster_size: int,
            min_samples: int | None,
            metric: str,
            cluster_selection_method: str,
            copy: bool,
        ) -> None:
            captured["min_cluster_size"] = min_cluster_size
            captured["min_samples"] = min_samples
            captured["metric"] = metric
            captured["cluster_selection_method"] = cluster_selection_method
            captured["copy"] = copy

        def fit_predict(self, matrix: list[list[float]]) -> list[int]:
            return [0 for _ in matrix]

    monkeypatch.setattr(
        clustering_mod,
        "_load_sklearn_tools",
        lambda: (FakeNumpy, FakeHDBSCAN, None),
    )

    result = cluster_entity_vectors(
        [_entity(f"entity-{index}", [1.0, float(index)]) for index in range(5)],
        project_id=None,
        min_cluster_size=5,
        min_samples=None,
    )

    assert captured["min_cluster_size"] == 5
    assert captured["min_samples"] is None
    assert result.cluster_count == 1


@pytest.mark.asyncio
async def test_recluster_project_entities_passes_density_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    entities = [_entity("entity-a", [1.0, 0.0])]

    class Reader:
        async def fetch_project_entity_vectors(self, project_id: str | None) -> list[EntityVector]:
            captured["read_project_id"] = project_id
            return entities

    class Writer:
        async def write_entity_clusters(
            self,
            labels: dict[str, int | None],
            project_id: str | None,
            *,
            is_global: bool,
        ) -> dict[str, int]:
            captured["labels"] = labels
            captured["write_project_id"] = project_id
            captured["write_is_global"] = is_global
            return {"clustered": 1, "noise": 0}

    def fake_cluster_entity_vectors(
        vector_rows: list[EntityVector],
        *,
        project_id: str | None,
        min_cluster_size: int,
        min_samples: int | None,
    ) -> ClusterRunResult:
        captured["vector_rows"] = vector_rows
        captured["cluster_project_id"] = project_id
        captured["min_cluster_size"] = min_cluster_size
        captured["min_samples"] = min_samples
        return ClusterRunResult(
            project_id=project_id,
            entity_count=1,
            valid_entity_count=1,
            clustered_entity_count=1,
            noise_count=0,
            invalid_count=0,
            cluster_count=1,
            cluster_ids_by_entity_key={"entity-a": 0},
            cluster_sizes={0: 1},
            invalid_entity_keys=[],
            quality_metrics={"silhouette": None},
        )

    monkeypatch.setattr(clustering_mod, "cluster_entity_vectors", fake_cluster_entity_vectors)

    result = await clustering_mod.recluster_project_entities(
        Reader(),  # type: ignore[arg-type]
        Writer(),  # type: ignore[arg-type]
        "project-1",
        is_global=False,
        min_cluster_size=7,
        min_samples=None,
    )

    assert result.cluster_count == 1
    assert captured["read_project_id"] == "project-1"
    assert captured["write_project_id"] == "project-1"
    assert captured["write_is_global"] is False
    assert captured["min_cluster_size"] == 7
    assert captured["min_samples"] is None


@pytest.mark.asyncio
async def test_recall_corpus_default_density_params_are_ari_optimum() -> None:
    from sklearn.metrics import adjusted_rand_score, homogeneity_score

    vectors = await direct_clustering_vectors(DIM)
    scores: dict[tuple[int, int | None], dict[str, float | int | None]] = {}

    for min_cluster_size in (2, 3, 5):
        for min_samples in (None, 1, 2):
            result = cluster_entity_vectors(
                vectors,
                project_id=None,
                min_cluster_size=min_cluster_size,
                min_samples=min_samples,
            )
            truth, predicted = _quality_labels(vectors, result)
            scores[(min_cluster_size, min_samples)] = {
                "ari": adjusted_rand_score(truth, predicted),
                "homogeneity": homogeneity_score(truth, predicted),
                "silhouette": result.quality_metrics["silhouette"],
                "clustered_entity_count": result.clustered_entity_count,
            }

    default = scores[(5, 2)]
    max_ari = max(float(score["ari"]) for score in scores.values())
    silhouettes = [
        float(score["silhouette"]) for score in scores.values() if score["silhouette"] is not None
    ]

    assert len(scores) == 9
    assert scores[(3, 2)]["clustered_entity_count"] == 91
    assert default["ari"] == pytest.approx(max_ari)
    assert default["homogeneity"] == pytest.approx(1.0)
    assert default["silhouette"] is not None
    assert float(default["silhouette"]) >= max(silhouettes) - 0.01


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
