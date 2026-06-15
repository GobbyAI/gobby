"""Offline entity-embedding clustering for the memory knowledge graph."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .reader import KnowledgeGraphReader
    from .writer import KnowledgeGraphWriter

DEFAULT_CLUSTER_MIN_CLUSTER_SIZE = 5
DEFAULT_CLUSTER_MIN_SAMPLES: int | None = 2
HDBSCAN_METRIC = "euclidean"
HDBSCAN_CLUSTER_SELECTION_METHOD = "eom"


@dataclass(frozen=True)
class EntityVector:
    """One KG entity and its stored embedding payload."""

    entity_key: str
    name: str
    embedding: Any


@dataclass(frozen=True)
class ClusterRunResult:
    """Summary of one offline entity reclustering run."""

    project_id: str | None
    entity_count: int
    valid_entity_count: int
    clustered_entity_count: int
    noise_count: int
    invalid_count: int
    cluster_count: int
    cluster_ids_by_entity_key: dict[str, int | None]
    cluster_sizes: dict[int, int]
    invalid_entity_keys: list[str]
    quality_metrics: dict[str, float | None]

    @property
    def cluster_summaries(self) -> list[dict[str, int]]:
        """Return deterministic cluster summaries for API responses."""
        return [
            {"cluster_id": cluster_id, "entity_count": size}
            for cluster_id, size in sorted(self.cluster_sizes.items())
        ]


def _load_sklearn_tools() -> tuple[Any, Any, Any]:
    try:
        import numpy as np
        from sklearn.cluster import HDBSCAN
        from sklearn.metrics import silhouette_score
    except ImportError as exc:
        raise ImportError(
            "scikit-learn>=1.5 is required to recluster knowledge graph entities; "
            "install the dev/test dependencies with `uv sync --group dev`."
        ) from exc
    return np, HDBSCAN, silhouette_score


def _coerce_unit_vector(embedding: Any) -> list[float] | None:
    if isinstance(embedding, (str, bytes)) or not isinstance(embedding, Sequence):
        return None
    values: list[float] = []
    for item in embedding:
        try:
            value = float(item)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        values.append(value)
    if not values:
        return None
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0.0:
        return None
    return [value / norm for value in values]


def _partition_valid_vectors(
    entities: Sequence[EntityVector],
) -> tuple[list[tuple[str, list[float]]], list[str]]:
    candidates: list[tuple[str, list[float]]] = []
    invalid_keys: list[str] = []
    for entity in entities:
        vector = _coerce_unit_vector(entity.embedding)
        if vector is None:
            invalid_keys.append(entity.entity_key)
            continue
        candidates.append((entity.entity_key, vector))

    if not candidates:
        return [], sorted(invalid_keys)

    dimension_counts = Counter(len(vector) for _, vector in candidates)
    expected_dim = sorted(
        dimension_counts.items(),
        key=lambda item: (-item[1], item[0]),
    )[0][0]

    valid: list[tuple[str, list[float]]] = []
    for entity_key, vector in candidates:
        if len(vector) != expected_dim:
            invalid_keys.append(entity_key)
            continue
        valid.append((entity_key, vector))
    return valid, sorted(invalid_keys)


def _canonicalize_labels(
    entity_keys: Sequence[str], labels: Sequence[int]
) -> dict[str, int | None]:
    clusters: dict[int, list[str]] = {}
    for entity_key, label in zip(entity_keys, labels, strict=True):
        label_int = int(label)
        if label_int < 0:
            continue
        clusters.setdefault(label_int, []).append(entity_key)

    ordered_clusters = sorted(
        clusters.items(),
        key=lambda item: tuple(sorted(item[1])),
    )
    canonical = {
        raw_label: cluster_id for cluster_id, (raw_label, _) in enumerate(ordered_clusters)
    }

    remapped: dict[str, int | None] = {}
    for entity_key, label in zip(entity_keys, labels, strict=True):
        label_int = int(label)
        remapped[entity_key] = canonical.get(label_int) if label_int >= 0 else None
    return remapped


def _cluster_sizes(labels_by_key: dict[str, int | None]) -> dict[int, int]:
    sizes: dict[int, int] = {}
    for cluster_id in labels_by_key.values():
        if cluster_id is None:
            continue
        sizes[cluster_id] = sizes.get(cluster_id, 0) + 1
    return sizes


def _noise_labels(entity_keys: Sequence[str]) -> dict[str, int | None]:
    return dict.fromkeys(entity_keys, None)


def _quality_metrics(
    *,
    matrix: Any | None,
    labels_by_key: dict[str, int | None],
    entity_keys: Sequence[str],
    silhouette_score: Any | None,
    entity_count: int,
    invalid_count: int,
) -> dict[str, float | None]:
    cluster_ids = [cluster_id for cluster_id in labels_by_key.values() if cluster_id is not None]
    clustered_count = len(cluster_ids)
    noise_count = len(labels_by_key) - clustered_count
    cluster_count = len(set(cluster_ids))
    silhouette: float | None = None

    if matrix is not None and silhouette_score is not None and cluster_count >= 2:
        clustered_indexes = [
            index
            for index, entity_key in enumerate(entity_keys)
            if labels_by_key[entity_key] is not None
        ]
        if len(clustered_indexes) > cluster_count:
            labels = [labels_by_key[entity_keys[index]] for index in clustered_indexes]
            clustered_matrix = matrix[clustered_indexes]
            silhouette = float(silhouette_score(clustered_matrix, labels, metric=HDBSCAN_METRIC))

    total = max(entity_count, 1)
    valid_count = len(labels_by_key)
    return {
        "silhouette": silhouette,
        "clustered_ratio": clustered_count / total,
        "noise_ratio": noise_count / total,
        "invalid_ratio": invalid_count / total,
        "valid_ratio": valid_count / total,
    }


def cluster_entity_vectors(
    entities: Sequence[EntityVector],
    *,
    project_id: str | None,
    min_cluster_size: int = DEFAULT_CLUSTER_MIN_CLUSTER_SIZE,
    min_samples: int | None = DEFAULT_CLUSTER_MIN_SAMPLES,
) -> ClusterRunResult:
    """Cluster entity vectors with deterministic labels and fail-clear dependency loading."""
    if min_cluster_size < 2:
        raise ValueError("min_cluster_size must be >= 2")
    if min_samples is not None and min_samples < 1:
        raise ValueError("min_samples must be None or >= 1")

    entity_count = len(entities)
    valid_vectors, invalid_entity_keys = _partition_valid_vectors(entities)
    valid_entity_keys = [entity_key for entity_key, _ in valid_vectors]
    invalid_count = len(invalid_entity_keys)

    if len(valid_vectors) < min_cluster_size:
        labels_by_key = _noise_labels(valid_entity_keys)
        labels_by_key.update(_noise_labels(invalid_entity_keys))
        metrics = _quality_metrics(
            matrix=None,
            labels_by_key=_noise_labels(valid_entity_keys),
            entity_keys=valid_entity_keys,
            silhouette_score=None,
            entity_count=entity_count,
            invalid_count=invalid_count,
        )
        return ClusterRunResult(
            project_id=project_id,
            entity_count=entity_count,
            valid_entity_count=len(valid_vectors),
            clustered_entity_count=0,
            noise_count=len(valid_vectors),
            invalid_count=invalid_count,
            cluster_count=0,
            cluster_ids_by_entity_key=labels_by_key,
            cluster_sizes={},
            invalid_entity_keys=invalid_entity_keys,
            quality_metrics=metrics,
        )

    np, HDBSCAN, silhouette_score = _load_sklearn_tools()
    matrix = np.asarray([vector for _, vector in valid_vectors], dtype=float)
    raw_labels = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric=HDBSCAN_METRIC,
        cluster_selection_method=HDBSCAN_CLUSTER_SELECTION_METHOD,
        copy=False,
    ).fit_predict(matrix)

    labels_by_key = _canonicalize_labels(valid_entity_keys, raw_labels)
    labels_by_key.update(_noise_labels(invalid_entity_keys))
    valid_labels_by_key = {
        entity_key: labels_by_key[entity_key] for entity_key in valid_entity_keys
    }
    sizes = _cluster_sizes(valid_labels_by_key)
    clustered_count = sum(sizes.values())
    metrics = _quality_metrics(
        matrix=matrix,
        labels_by_key=valid_labels_by_key,
        entity_keys=valid_entity_keys,
        silhouette_score=silhouette_score,
        entity_count=entity_count,
        invalid_count=invalid_count,
    )

    return ClusterRunResult(
        project_id=project_id,
        entity_count=entity_count,
        valid_entity_count=len(valid_vectors),
        clustered_entity_count=clustered_count,
        noise_count=len(valid_vectors) - clustered_count,
        invalid_count=invalid_count,
        cluster_count=len(sizes),
        cluster_ids_by_entity_key=labels_by_key,
        cluster_sizes=sizes,
        invalid_entity_keys=invalid_entity_keys,
        quality_metrics=metrics,
    )


async def recluster_project_entities(
    reader: KnowledgeGraphReader,
    writer: KnowledgeGraphWriter,
    project_id: str | None,
    *,
    min_cluster_size: int = DEFAULT_CLUSTER_MIN_CLUSTER_SIZE,
    min_samples: int | None = DEFAULT_CLUSTER_MIN_SAMPLES,
) -> ClusterRunResult:
    """Read entity embeddings, run HDBSCAN, and persist canonical cluster IDs."""
    entities = await reader.fetch_project_entity_vectors(project_id=project_id)
    result = cluster_entity_vectors(
        entities,
        project_id=project_id,
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
    )
    await writer.write_entity_clusters(
        result.cluster_ids_by_entity_key,
        project_id=project_id,
    )
    return result
