"""Shared deterministic corpus helpers for memory recall benchmarks."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from gobby.memory.identity import entity_key
from gobby.memory.services.knowledge_graph.clustering import EntityVector
from gobby.memory.services.knowledge_graph.models import Entity, Relationship
from gobby.memory.services.knowledge_graph.normalization import display_entity_name
from gobby.storage.projects import PERSONAL_PROJECT_ID

DIM = 16
NUM_CLUSTERS = 5
MEMORIES_PER_CLUSTER = 6
DISTRACTORS_PER_CLUSTER = 6
K = 5  # recall@k / cutoff for the labeled query set
NOISE_DIM = DIM - 1  # orthogonal axis distractors load onto


@dataclass
class MemoryDef:
    memory_id: str
    cluster: int
    entities: list[str]
    typed_pairs: list[tuple[str, str]] = field(default_factory=list)


def _hub(c: int) -> str:
    return f"c{c}_hub"


def _spoke(c: int, i: int, j: int) -> str:
    return f"c{c}_m{i}_s{j}"


def _distractor(c: int, n: int) -> str:
    return f"c{c}_noise_{n}"


def build_corpus() -> list[MemoryDef]:
    """Clustered memories where CO_OCCURS exposes cross-memory hub paths."""
    memories: list[MemoryDef] = []
    for c in range(NUM_CLUSTERS):
        for i in range(MEMORIES_PER_CLUSTER):
            s0, s1 = _spoke(c, i, 0), _spoke(c, i, 1)
            memories.append(
                MemoryDef(
                    memory_id=f"mem_c{c}_m{i}",
                    cluster=c,
                    entities=[_hub(c), s0, s1],
                    typed_pairs=[(s0, s1)],
                )
            )
        noise = [_distractor(c, n) for n in range(DISTRACTORS_PER_CLUSTER)]
        memories.append(
            MemoryDef(
                memory_id=f"mem_c{c}_noise",
                cluster=c,
                entities=[_hub(c), *noise],
                typed_pairs=[],
            )
        )
    return memories


def sorted_entity_names() -> list[str]:
    """Return every unique corpus entity name in deterministic order."""
    return sorted({name for memory in build_corpus() for name in memory.entities})


def _cluster_of(name: str) -> int:
    # names start with "c<digit(s)>_"
    return int(name[1 : name.index("_")])


def ground_truth_entity_clusters() -> dict[str, int | None]:
    """Ground-truth entity labels for clustering quality metrics."""
    return {
        name: None if "_noise_" in name else _cluster_of(name) for name in sorted_entity_names()
    }


def make_embed_fn(dim: int, *, unique_signal: float = 0.0) -> Any:
    """Deterministic embeddings: cluster onehot for hubs/spokes, noise axis for distractors."""

    def _jitter(name: str) -> float:
        digest = int(sha256(name.encode()).hexdigest(), 16)
        return ((digest % 1000) / 1000.0) * 0.05

    def _unique_components(name: str) -> list[float]:
        vec = [0.0] * dim
        if unique_signal <= 0.0 or dim <= 6:
            return vec
        digest = sha256(name.encode()).digest()
        usable_width = max(NOISE_DIM - 5, 1)
        for offset in range(2):
            axis = 5 + (digest[offset] % usable_width)
            sign = 1.0 if digest[offset + 2] % 2 == 0 else -1.0
            magnitude = unique_signal * (0.75 + (digest[offset + 4] / 255.0) * 0.5)
            vec[axis] += sign * magnitude
        return vec

    async def embed(name: str, is_query: bool = False) -> list[float]:
        vec = [0.0] * dim
        cluster = _cluster_of(name)
        if "_noise_" in name:
            vec[NOISE_DIM] = 1.0
            vec[cluster % NOISE_DIM] = _jitter(name)
        else:
            vec[cluster % NOISE_DIM] = 1.0
            vec[(cluster + 1) % NOISE_DIM] += _jitter(name)
            for index, component in enumerate(_unique_components(name)):
                vec[index] += component
        return vec

    return embed


async def direct_clustering_vectors(dim: int = DIM) -> list[EntityVector]:
    """Build EntityVector rows from the same deterministic embeddings used in benchmarks."""
    embed = make_embed_fn(dim)
    return [
        EntityVector(
            entity_key=entity_key(PERSONAL_PROJECT_ID, display_entity_name(name)),
            name=display_entity_name(name),
            embedding=await embed(name),
        )
        for name in sorted_entity_names()
    ]


def _seed_keys(mem: MemoryDef) -> list[str]:
    return [entity_key(PERSONAL_PROJECT_ID, display_entity_name(name)) for name in mem.entities]


class _StubExtractor:
    def __init__(self, by_content: dict[str, MemoryDef]) -> None:
        self._by_content = by_content

    async def extract_entities(self, content: str) -> list[Entity]:
        mem = self._by_content[content]
        return [Entity(name=name, entity_type="concept") for name in mem.entities]

    async def extract_relationships(
        self, content: str, entities: list[Entity]
    ) -> list[Relationship]:
        mem = self._by_content[content]
        return [
            Relationship(source=s, target=t, relationship="RELATED_TO") for s, t in mem.typed_pairs
        ]

    async def select_outdated_relations(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []


class _Stub:
    """Placeholder for the unused prompt_loader / llm_service constructor args."""
